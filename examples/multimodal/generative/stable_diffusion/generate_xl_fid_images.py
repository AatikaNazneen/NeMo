import os
import numpy as np
from einops import rearrange
from omegaconf.omegaconf import open_dict
from PIL import Image

from nemo.collections.multimodal.parts.stable_diffusion.sdxl_pipeline import SamplingPipeline
from nemo.core.config import hydra_runner


@hydra_runner(config_path='conf', config_name='sd_xl_fid_images')
def main(cfg):
    # Read configuration parameters
    nnodes_per_cfg = cfg.fid.nnodes_per_cfg
    ntasks_per_node = cfg.fid.ntasks_per_node
    local_task_id = cfg.fid.local_task_id
    num_images_to_eval = cfg.fid.num_images_to_eval
    path = cfg.fid.coco_captions_path
    use_refiner = cfg.get('use_refiner', False)

    node_id = int(os.environ.get("SLURM_ARRAY_TASK_ID", 0))
    node_id_per_cfg = node_id % nnodes_per_cfg

    current_node_cfg = cfg.fid.classifier_free_guidance[node_id // nnodes_per_cfg]
    with open_dict(cfg):
        cfg.sampling.base.scale = current_node_cfg
        if use_refiner:
            cfg.sampling.refiner.scale = current_node_cfg
    save_path = os.path.join(cfg.fid.save_path, str(current_node_cfg))

    # Read and store captions
    captions = []
    caption_files = sorted(os.listdir(path))
    assert len(caption_files) >= num_images_to_eval
    for file in caption_files[:num_images_to_eval]:
        with open(os.path.join(path, file), 'r') as f:
            captions += f.readlines()

    # Calculate partition sizes and select the partition for the current node
    partition_size_per_node = num_images_to_eval // nnodes_per_cfg
    start_idx = node_id_per_cfg * partition_size_per_node
    end_idx = (node_id_per_cfg + 1) * partition_size_per_node if node_id_per_cfg != nnodes_per_cfg - 1 else None
    captions = captions[start_idx:end_idx]

    local_task_id = int(local_task_id) if local_task_id is not None else int(os.environ.get("SLURM_LOCALID", 0))
    partition_size_per_task = int(len(captions) // ntasks_per_node)

    # Select the partition for the current task
    start_idx = local_task_id * partition_size_per_task
    end_idx = (local_task_id + 1) * partition_size_per_task if local_task_id != ntasks_per_node - 1 else None
    input = captions[start_idx:end_idx]

    print(f"Current worker {node_id}:{local_task_id} will generate {len(input)} images")

    os.makedirs(save_path, exist_ok=True)

    base_model_config = cfg.base_model_config
    base = SamplingPipeline(base_model_config, use_fp16=cfg.use_fp16)
    if use_refiner:
        refiner_config = cfg.refiner_config
        refiner = SamplingPipeline(refiner_config, use_fp16=cfg.use_fp16)

    # Generate images using the model and save them
    for i, prompt in enumerate(input):
        cfg.infer.prompt = [prompt]
        output = base.text_to_image(
            params=cfg.sampling.base,
            prompt=cfg.infer.prompt,
            negative_prompt=cfg.infer.negative_prompt,
            samples=cfg.infer.num_samples,
            return_latents=True if use_refiner else False,
            seed=cfg.infer.seed,
        )

        if use_refiner:
            assert isinstance(samples, (tuple, list))
            samples, samples_z = samples
            assert samples is not None
            assert samples_z is not None

            # perform_save_locally(cfg.out_path, samples)

            output = refiner.refiner(
                params=cfg.sampling.refiner,
                image=samples_z,
                prompt=cfg.infer.prompt,
                negative_prompt=cfg.infer.negative_prompt,
                samples=cfg.infer.num_samples,
                seed=cfg.infer.seed,
            )

        for sample in output:
            image_num = i + partition_size_per_node * node_id_per_cfg + partition_size_per_task * local_task_id
            sample = 255.0 * rearrange(sample.cpu().numpy(), "c h w -> h w c")
            image = Image.fromarray(sample.astype(np.uint8))
            image.save(os.path.join(save_path, f'image{image_num:06d}.png'))


if __name__ == "__main__":
    main()
