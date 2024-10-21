import os
from collections import namedtuple
from pathlib import Path
import warnings

from detectron2.export import TracingAdapter
from detectron2.structures import Instances, Boxes
from diffusers.image_processor import VaeImageProcessor
from huggingface_hub import snapshot_download
import yaml
import openvino as ov
import torch

from model.cloth_masker import AutoMasker
from model.pipeline import CatVTONPipeline


def convert(model: torch.nn.Module, xml_path: str, example_input):
    xml_path = Path(xml_path)
    if not xml_path.exists():
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        model.eval()
        with torch.no_grad():
            converted_model = ov.convert_model(model, example_input=example_input)
        ov.save_model(converted_model, xml_path)

        # cleanup memory
        torch._C._jit_clear_class_registry()
        torch.jit._recursive.concrete_type_store = torch.jit._recursive.ConcreteTypeStore()
        torch.jit._state._clear_class_state()


class VaeEncoder(torch.nn.Module):
    def __init__(self, vae):
        super().__init__()
        self.vae = vae

    def forward(self, x):
        return self.vae.encode(x).latent_dist.sample()


class VaeDecoder(torch.nn.Module):
    def __init__(self, vae):
        super().__init__()
        self.vae = vae

    def forward(self, latents):
        return self.vae.decode(latents)


class UNetWrapper(torch.nn.Module):
    def __init__(self, unet):
        super().__init__()
        self.unet = unet

    def forward(self, sample=None, timestep=None, encoder_hidden_states=None, return_dict=None):
        result = self.unet(sample=sample, timestep=timestep, encoder_hidden_states=encoder_hidden_states, return_dict=False)
        return result


def download_models():
    resume_path = "zhengchong/CatVTON"
    base_model_path = "booksforcharlie/stable-diffusion-inpainting"
    repo_path = snapshot_download(repo_id=resume_path)

    pipeline = CatVTONPipeline(base_ckpt=base_model_path, attn_ckpt=repo_path, attn_ckpt_version="mix", use_tf32=True, device="cpu")

    # fix default config to use cpu
    with open(f"{repo_path}/DensePose/densepose_rcnn_R_50_FPN_s1x.yaml", "r") as fp:
        data = yaml.safe_load(fp)

    data["MODEL"].update({"DEVICE": "cpu"})

    with open(f"{repo_path}/DensePose/densepose_rcnn_R_50_FPN_s1x.yaml", "w") as fp:
        yaml.safe_dump(data, fp)

    mask_processor = VaeImageProcessor(vae_scale_factor=8, do_normalize=False, do_binarize=True, do_convert_grayscale=True)
    automasker = AutoMasker(
        densepose_ckpt=os.path.join(repo_path, "DensePose"),
        schp_ckpt=os.path.join(repo_path, "SCHP"),
        device="cpu",
    )
    return pipeline, mask_processor, automasker


def convert_pipeline_models(pipeline, vae_encoder_path, vae_decoder_path, unet_path):
    convert(VaeEncoder(pipeline.vae), vae_encoder_path, torch.zeros(1, 3, 1024, 768))
    convert(VaeDecoder(pipeline.vae), vae_decoder_path, torch.zeros(1, 4, 128, 96))

    inpainting_latent_model_input = torch.zeros(2, 9, 256, 96)
    timestep = torch.tensor(0)
    encoder_hidden_states = torch.zeros(2, 1, 768)
    example_input = (inpainting_latent_model_input, timestep, encoder_hidden_states)

    convert(UNetWrapper(pipeline.unet), unet_path, example_input)


def convert_automasker_models(automasker, densepose_processor_path, schp_processor_atr_path, schp_processor_lip_path):
    def inference(model, inputs):
        # use do_postprocess=False so it returns ROI mask
        inst = model.inference(inputs, do_postprocess=False)[0]
        return [{"instances": inst}]

    tracing_input = [{"image": torch.rand([3, 800, 800], dtype=torch.float32)}]
    warnings.filterwarnings("ignore")
    traceable_model = TracingAdapter(automasker.densepose_processor.predictor.model, tracing_input, inference)

    convert(traceable_model, densepose_processor_path, tracing_input[0]["image"])

    convert(automasker.schp_processor_atr.model, schp_processor_atr_path, torch.rand([1, 3, 512, 512], dtype=torch.float32))
    convert(automasker.schp_processor_lip.model, schp_processor_lip_path, torch.rand([1, 3, 473, 473], dtype=torch.float32))


class VAEWrapper(torch.nn.Module):
    def __init__(self, vae_encoder, vae_decoder, config):
        super().__init__()
        self.vae_enocder = vae_encoder
        self.vae_decoder = vae_decoder
        self.device = "cpu"
        self.dtype = torch.float32
        self.config = config

    def encode(self, pixel_values):
        outs = self.vae_enocder(pixel_values)
        outs = torch.from_numpy(outs[0])
        result = namedtuple("VAE", "latent_dist")(namedtuple("Sample", "sample")(lambda: outs))
        return result

    def decode(self, latents):
        outs = self.vae_decoder(latents)
        outs = namedtuple("VAE", "sample")(torch.from_numpy(outs[0]))
        return outs


class ConvUnetWrapper(torch.nn.Module):
    def __init__(self, unet):
        super().__init__()
        self.unet = unet

    def forward(self, sample, timestep, encoder_hidden_states=None, **kwargs):
        outputs = self.unet(
            {
                "sample": sample,
                "timestep": timestep,
            },
        )

        return [torch.from_numpy(outputs[0])]


class ConvDenseposeProcessorWrapper(torch.nn.Module):
    def __init__(self, densepose_processor):
        super().__init__()
        self.densepose_processor = densepose_processor

    def forward(self, sample, **kwargs):
        outputs = self.densepose_processor(sample[0]["image"])
        boxes = outputs[0]
        classes = outputs[1]
        has_mask = len(outputs) >= 5
        scores = outputs[2 if not has_mask else 3]
        print(scores)
        model_input_size = (
            int(outputs[3 if not has_mask else 4][0]),
            int(outputs[3 if not has_mask else 4][1]),
        )
        filtered_detections = scores >= 0
        boxes = Boxes(boxes[filtered_detections])
        scores = scores[filtered_detections]
        classes = classes[filtered_detections]
        out_dict = {"pred_boxes": boxes, "scores": scores, "pred_classes": classes}

        instances = Instances(model_input_size, **out_dict)

        return [{"instances": instances}]


class ConvSchpProcessorWrapper(torch.nn.Module):
    def __init__(self, schp_processor):
        super().__init__()
        self.schp_processor = schp_processor

    def forward(self, image):
        outputs = self.schp_processor(image)

        return torch.from_numpy(outputs[0])


def get_compiled_pipeline(pipeline, core, device, vae_encoder_path, vae_decoder_path, unet_path):
    compiled_unet = core.compile_model(unet_path, device.value)
    compiled_vae_encoder = core.compile_model(vae_encoder_path, device.value)
    compiled_vae_decoder = core.compile_model(vae_decoder_path, device.value)

    pipeline.vae = VAEWrapper(compiled_vae_encoder, compiled_vae_decoder, pipeline.vae.config)
    pipeline.unet = ConvUnetWrapper(compiled_unet)

    return pipeline


def get_compiled_automasker(automasker, core, device, densepose_processor_path, schp_processor_atr_path, schp_processor_lip_path):
    compiled_densepose_processor = core.compile_model(densepose_processor_path, device.value)
    compiled_schp_processor_atr = core.compile_model(schp_processor_atr_path, device.value)
    compiled_schp_processor_lip = core.compile_model(schp_processor_lip_path, device.value)

    automasker.densepose_processor.predictor.model = ConvDenseposeProcessorWrapper(compiled_densepose_processor)
    automasker.schp_processor_atr.model = ConvSchpProcessorWrapper(compiled_schp_processor_atr)
    automasker.schp_processor_lip.model = ConvSchpProcessorWrapper(compiled_schp_processor_lip)

    return automasker
