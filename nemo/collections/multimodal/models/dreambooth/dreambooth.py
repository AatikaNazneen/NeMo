# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import numpy as np
import os
import pytorch_lightning as pl
import torch
from abc import ABC
from apex import amp
from apex.contrib.clip_grad import clip_grad_norm_
from functools import partial
from omegaconf import DictConfig, OmegaConf, open_dict
from pytorch_lightning import Trainer
from pytorch_lightning.utilities import GradClipAlgorithmType
from torch._dynamo import optimize
from torch._inductor import config as inductor_config
from torch.optim.lr_scheduler import LambdaLR
from typing import Any, Dict, Optional, Union

from nemo.collections.multimodal.data.dreambooth.dreambooth_dataset import DreamBoothDataset
from nemo.collections.multimodal.models.multimodal_base_model import MegatronMultimodalModel
from nemo.collections.multimodal.modules.stable_diffusion.diffusionmodules.util import make_beta_schedule, \
    extract_into_tensor, noise_like
from nemo.collections.multimodal.parts.stable_diffusion.utils import default, exists
from nemo.collections.multimodal.parts.utils import randn_like
from nemo.collections.nlp.data.language_modeling.megatron.megatron_batch_samplers import (
    MegatronPretrainingRandomBatchSampler,
)
from nemo.collections.nlp.parts.utils_funcs import get_last_rank, is_last_rank
from nemo.core.classes import ModelPT
from nemo.core.classes.common import Serialization
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager

try:
    from apex.contrib.clip_grad import clip_grad_norm_
    from apex import amp
    from apex.transformer import parallel_state
    from apex.transformer.pipeline_parallel.schedules.common import build_model
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_without_interleaving import (
        forward_backward_pipelining_without_interleaving,
    )
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_with_interleaving import (
        _forward_backward_pipelining_with_interleaving,
    )
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_no_pipelining import forward_backward_no_pipelining
    from apex.transformer.enums import AttnMaskType

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


def disabled_train(self, mode=True):
    """Overwrite model.train with this function to make sure train/eval mode
    does not change anymore."""
    return self


def _collate_fn(examples, with_prior_preservation=False):
    if with_prior_preservation:
        prompts = [
            [example["instance_prompt"], example["reg_prompt"]]
            for example in examples
        ]
        images = [example["instance_images"] for example in examples] + \
                 [example["reg_images"] for example in examples]
    else:
        prompts = [[example["instance_prompt"]] for example in examples]
        images = [example["instance_images"] for example in examples]

    images = torch.stack(images)
    images = images.to(memory_format=torch.contiguous_format).float()

    return prompts, images


class DreamBooth(torch.nn.Module, Serialization):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.with_prior_preservation = self.cfg.with_prior_preservation
        self.num_reg_images = self.cfg.data.num_reg_images
        self.pretrained_ckpt = self.cfg.pretrained_ckpt
        self.prior_loss_weight = self.cfg.prior_loss_weight
        self.num_images_per_prompt = self.cfg.data.num_images_per_prompt

        self.train_text_encoder = self.cfg.train_text_encoder
        self.instantiate_text_encoder(self.cfg.cond_stage_config)

        self.inductor = self.cfg.inductor
        self.inductor_cudagraphs = self.cfg.inductor_cudagraphs

        self.instantiate_vae(self.cfg.first_stage_config)
        self.instantiate_unet(self.cfg.unet_config)

        self.scale_factor = self.cfg.scale_factor
        self.num_timesteps = self.cfg.noise_scheduler.timesteps
        self.parameterization = self.cfg.noise_scheduler.parameterization
        self.get_noise_scheduler(self.cfg.noise_scheduler)

        self.rng = torch.Generator(device=torch.cuda.current_device(), )

    def instantiate_unet(self, cfg):
        self.unet = DreamBooth.from_config_dict(cfg)
        self.unet.train()
        if self.inductor:
            # TorchInductor with CUDA graph can lead to OOM
            inductor_config.triton.cudagraphs = cfg.inductor_cudagraphs
            self.unet = optimize("inductor")(self.unet)

    def instantiate_vae(self, cfg):
        model = DreamBooth.from_config_dict(cfg)
        self.vae = model.eval()
        self.vae.train = disabled_train
        for param in self.vae.parameters():
            param.requires_grad = False

    def instantiate_text_encoder(self, cfg):
        model = DreamBooth.from_config_dict(cfg)
        if self.train_text_encoder:
            self.text_encoder = model.train()
            for param in self.text_encoder.parameters():
                param.requires_grad = True
        else:
            self.text_encoder = model.eval()
            self.text_encoder.train = disabled_train
            for param in self.text_encoder.parameters():
                param.requires_grad = False

    def get_noise_scheduler(self, cfg):
        model = DreamBooth.from_config_dict(cfg)
        self.noise_scheduler = model.eval()

    def forward(self, batch):
        x, cond = batch

        latents = self.vae.encode(x).sample().detach()
        latents = latents * self.scale_factor

        noise = randn_like(latents, generator=self.rng)
        t = torch.randint(0, self.num_timesteps, (latents.shape[0],), generator=self.rng, device=latents.device).long()
        x_noisy = self.noise_scheduler(x_start=latents, t=t, noise=noise)

        # cond = self.text_encoder([t[0] for t in batch["prompts"]])
        # if self.with_prior_preservation:
        #     cond_prior = self.text_encoder([t[1] for t in batch["prompts"]])
        #     cond = torch.cat([cond, cond_prior], dim=0)

        model_output = self.unet(x_noisy, t, cond)

        if self.parameterization == "x0":
            target = latents
        elif self.parameterization == "eps":
            target = noise
        else:
            raise NotImplementedError()

        if self.with_prior_preservation:
            model_pred, model_pred_prior = torch.chunk(model_output, 2, dim=0)
            target, target_prior = torch.chunk(target, 2, dim=0)
            loss = torch.nn.functional.mse_loss(model_pred.float(), target.float(), reduction="mean")
            prior_loss = torch.nn.functional.mse_loss(model_pred_prior.float(), target_prior.float(), reduction="mean")
            loss = loss + prior_loss * self.prior_loss_weight

        else:
            loss = torch.nn.functional.mse_loss(target.float(), model_output.float(), reduction="mean")

        return loss

    def parameters(self):
        params = list(self.unet.parameters())
        if self.train_text_encoder:
            # print(f"{self.__class__.__name__}: Also optimizing conditioner params!")
            params = params + list(self.text_encoder.parameters())
        return params

    def set_input_tensor(self, input_tensor):
        """See megatron.model.transformer.set_input_tensor()"""
        pass


class MegatronDreamBooth(MegatronMultimodalModel):

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )

        # this prevents base constructor from initializing tokenizer
        self.tokenizer = None
        super().__init__(cfg, trainer=trainer)

        self._validate_trainer()

        # megatron_amp_O2 is not yet supported in diffusion models
        self.megatron_amp_O2 = cfg.get('megatron_amp_O2', False)
        self.model = self.model_provider_func()

        if self.trainer.precision == 'bf16':
            self.autocast_dtype = torch.bfloat16
        elif int(self.trainer.precision) == 32:
            self.autocast_dtype = torch.float
        elif int(self.trainer.precision) == 16:
            self.autocast_dtype = torch.half
        else:
            raise ValueError('precision must be in [32, 16, "bf16"]')

    def model_provider_func(self, pre_process=True, post_process=True):
        """Model depends on pipeline paralellism."""
        model = DreamBooth(cfg=self.cfg)
        return model

    def forward(self, batch):
        output_tensor = self.model(batch)
        return output_tensor

    def training_step(self, batch, batch_idx):
        """
            Our dataloaders produce a micro-batch and then we fetch
            a number of microbatches depending on the global batch size and model parallel size
            from the dataloader to produce a list of microbatches.
            Batch should be a list of microbatches and those microbatches should on CPU.
            Microbatches are then moved to GPU during the pipeline.
            The list of microbatches is then piped through the pipeline using Apex fwd/bwd functions.
        """

        # we zero grads here because we also call backward in the apex fwd/bwd functions
        self._optimizer.zero_grad()

        # we prepare the micro batches for the apex fwd/bwd function
        batch_for_pipeline = self.process_global_batch(batch)

        # run forward and backwards passes for an entire global batch
        # we do this inside training_step to support pipeline parallelism
        losses_reduced_per_micro_batch = forward_backward_no_pipelining(
            forward_step_func=self.get_forward_output_and_loss_func(),
            batch=batch_for_pipeline,
            model=self.model,
            forward_only=False,
            tensor_shape=None,  # required by pipeline parallelism
            dtype=self.autocast_dtype,
            grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
            custom_sync_context_handler=None,
            sequence_parallel_enabled=False,
            sync_batch_comm=False,
        )

        # only the last stages of the pipeline return losses
        loss_dict = {}
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['loss'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.stack(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            loss_mean = torch.tensor(0.0, device=torch.cuda.current_device())

        # when using sequence parallelism, the sequence parallel layernorm grads must be all-reduced
        if self.cfg.get('tensor_model_parallel_size', 1) > 1 and self.cfg.get('sequence_parallel', False):
            self.allreduce_sequence_parallel_gradients()

        if self.with_distributed_adam:
            # gradients are reduced internally in distributed optimizer
            pass
        elif self.megatron_amp_O2:
            # # when using pipeline parallelism grads must be all-reduced after the pipeline (not asynchronously)
            # if self.cfg.get('pipeline_model_parallel_size', 1) > 1 or self.cfg.get('sequence_parallel', False):
            #     # main grads are stored in the MainParamsOptimizer wrapper
            #     self._optimizer.allreduce_main_grads()
            self._optimizer.allreduce_main_grads()
        else:
            # async grad allreduce is not currently implemented for O1/autocasting mixed precision training
            # so we all-reduce gradients after the pipeline
            self.allreduce_gradients()  # @sangkug we think this is causing memory to blow up (hurts perf)

        torch.distributed.broadcast(loss_mean, get_last_rank())

        if self.cfg.precision == 16:
            loss_scale = self.trainer.precision_plugin.scaler._scale
            if loss_scale is not None:
                self.log('loss_scale', loss_scale)

        self.log('reduced_train_loss', loss_mean, prog_bar=True, rank_zero_only=True)
        lr = self._optimizer.param_groups[0]['lr']
        self.log('lr', lr, prog_bar=True, rank_zero_only=True)
        self.log('global_step', self.trainer.global_step + 1, prog_bar=True, rank_zero_only=True)
        self.log(
            'consumed_samples',
            self.compute_consumed_samples(self.trainer.global_step + 1 - self.init_global_step),
            prog_bar=True,
            rank_zero_only=True,
        )
        return loss_mean

    @torch.no_grad()
    def validation_step(self, batch, batch_idx):
        batch_for_pipeline = self.process_global_batch(batch)

        losses_reduced_per_micro_batch = forward_backward_no_pipelining(
            forward_step_func=self.get_forward_output_and_loss_func(),
            batch=batch_for_pipeline,
            model=self.model,
            forward_only=True,
            tensor_shape=None,  # required by pipeline parallelism
            dtype=self.autocast_dtype,
        )
        # only the last stages of the pipeline return losses
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['loss'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.stack(loss_tensors_list)
            val_loss_mean = loss_tensor.mean()
        else:
            val_loss_mean = torch.tensor(0.0, device=torch.cuda.current_device())

        self.log(val_loss_mean, prog_bar=False, logger=True, on_step=False, on_epoch=True)

    def backward(self, *args, **kwargs):
        """ LightningModule hook to do backward.
            We want this to do nothing since we run backward in the fwd/bwd functions from apex.
            No need to call it here.
        """
        pass

    def optimizer_zero_grad(self, *args, **kwargs):
        """ LightningModule hook to zero grad.
            We want this to do nothing as we are zeroing grads during the training_step.
        """
        pass

    def _append_module_grads(self, module, grads):
        for param in module.parameters():
            if getattr(param, 'sequence_parallel_enabled', False):
                if self.megatron_amp_O2:
                    grad = param.main_grad
                else:
                    grad = param.grad
                grads.append(grad.data)

    def process_global_batch(self, global_batch, global_batch_size=None):
        """ Prepares the global batch for apex fwd/bwd functions.
            Global batch is a list of micro batches.
        """
        # noise_map, condition
        prompts, images = global_batch

        # DB has more dedicated structure for encoding, so we enable autocasting here as well
        with torch.cuda.amp.autocast(
                self.autocast_dtype in (torch.half, torch.bfloat16),
                dtype=self.autocast_dtype,
        ):
            images = images.cuda(non_blocking=True)

            cond = self.model.text_encoder([t[0] for t in prompts])
            if self.cfg.with_prior_preservation:
                cond_prior = self.model.text_encoder([t[1] for t in prompts])
                cond = torch.cat([cond, cond_prior], dim=0)

        return images, cond

    def get_forward_output_and_loss_func(self):
        def fwd_output_and_loss_func(batch, model):
            batch = [x.cuda(non_blocking=True) for x in batch]
            loss = model(batch)

            def dummy(output_tensor):
                return loss, {'loss': loss}

            return loss, dummy

        return fwd_output_and_loss_func

    def get_forward_output_only_func(self):
        def fwd_output_only_func(batch, model):
            raise NotImplementedError

        return fwd_output_only_func

    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """
        self.model.rng.manual_seed(self.cfg.seed + 100 * parallel_state.get_data_parallel_rank())

        # log number of parameters
        if isinstance(self.model, list):
            num_parameters_on_device = sum(
                [sum([p.nelement() for p in model_module.parameters()]) for model_module in self.model]
            )
        else:
            num_parameters_on_device = sum([p.nelement() for p in self.model.parameters()])

        # to be summed across data parallel group
        total_num_parameters = torch.tensor(num_parameters_on_device).cuda(non_blocking=True)

        torch.distributed.all_reduce(total_num_parameters, group=parallel_state.get_model_parallel_group())

        logging.info(
            f'Pipeline model parallel rank: {parallel_state.get_pipeline_model_parallel_rank()}, '
            f'Tensor model parallel rank: {parallel_state.get_tensor_model_parallel_rank()}, '
            f'Number of model parameters on device: {num_parameters_on_device:.2e}. '
            f'Total number of model parameters: {total_num_parameters:.2e}.'
        )

        resume_checkpoint_path = self.trainer._checkpoint_connector.resume_from_checkpoint_fit_path
        if resume_checkpoint_path:
            init_consumed_samples = self._extract_consumed_samples_from_ckpt(resume_checkpoint_path)
        else:
            init_consumed_samples = 0
        self.init_consumed_samples = init_consumed_samples
        self.init_global_step = self.trainer.global_step

        # Batch size need to be provided for webdatset
        self._num_micro_batches = self.cfg.global_batch_size // (
                self.cfg.micro_batch_size * parallel_state.get_data_parallel_world_size())
        self._global_batch_size_on_this_data_parallel_rank = self._num_micro_batches * self.cfg.micro_batch_size

        self.setup_training_data(self.cfg.data)

    def setup_training_data(self, cfg):
        if self.cfg.with_prior_preservation:
            if cfg.regularization_dir is None:
                raise ValueError("Regularization images must be provided to train with prior preservation loss")
            if cfg.regularization_prompt is None:
                raise ValueError("Regularization prompts must be provided to train with prior preservation loss")

        train_dataset = DreamBoothDataset(
            instance_data_root=cfg.instance_dir,
            instance_prompt=cfg.instance_prompt,
            reg_data_root=cfg.regularization_dir if self.cfg.with_prior_preservation else None,
            reg_prompt=cfg.regularization_prompt if self.cfg.with_prior_preservation else None,
            size=cfg.resolution,
            center_crop=cfg.center_crop,
        )

        batch_sampler = MegatronPretrainingRandomBatchSampler(
            total_samples=len(train_dataset),
            consumed_samples=self.compute_consumed_samples(0),
            micro_batch_size=self.cfg.micro_batch_size,
            global_batch_size=self.cfg.global_batch_size,
            data_parallel_rank=parallel_state.get_data_parallel_rank(),
            data_parallel_size=parallel_state.get_data_parallel_world_size(),
            drop_last=False,
        )

        self._train_dl = torch.utils.data.DataLoader(
            train_dataset,
            # batch_size=self._global_batch_size_on_this_data_parallel_rank,
            batch_sampler=batch_sampler,
            collate_fn=partial(_collate_fn, with_prior_preservation=self.cfg.with_prior_preservation),
            num_workers=cfg.num_workers,
            pin_memory=True,
            persistent_workers=True,
        )

    def setup_validation_data(self, cfg):
        pass

    def setup_test_data(self, cfg):
        pass

    def transfer_batch_to_device(self, batch: Any, device: torch.device, dataloader_idx: int) -> Any:
        """ PTL hook: https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#transfer-batch-to-device
            When using pipeline parallelism, we need the global batch to remain on the CPU,
            since the memory overhead will be too high when using a large number of microbatches.
            Microbatches are transferred from CPU to GPU inside the pipeline.
        """
        return batch

    def _validate_trainer(self):
        """ Certain trainer configurations can break training.
            Here we try to catch them and raise an error.
        """
        if self.trainer.accumulate_grad_batches > 1:
            raise ValueError(
                f'Gradient accumulation is done within training_step. trainer.accumulate_grad_batches must equal 1'
            )

    @classmethod
    def list_available_models(cls):
        return None

    def parameters(self):
        if isinstance(self.model, list):
            return itertools.chain.from_iterable(module.parameters() for module in self.model)
        else:
            return self.model.parameters()
