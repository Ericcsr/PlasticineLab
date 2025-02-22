# Need to use the optimzier from pytorch
from argparse import Namespace
import copy
import os

import taichi as ti
import torch
from torch.utils.data.dataloader import DataLoader
from typing import Any, Tuple, Type, Union
from yacs.config import CfgNode as CN

from .optim import Optimizer
from .. import mpi
from ..config.utils import make_cls_config
from ..engine import taichi_env
from ..engine.losses import compute_emd
from ..engine.losses import state_loss, emd_loss, chamfer_loss, loss, solve_icp
from ..engine.taichi_env import TaichiEnv
from ..envs import make
from ..neurals.autoencoder import PCNAutoEncoder
from ..neurals.pcdataloader import ChopSticksDataset, RopeDataset, WriterDataset, TorusDataset

HIDDEN_LAYERS = 256
LATENT_DIMS   = 1024
FEAT_DMIS     = 3

torch.set_num_threads(64)
mpi.setup_pytorch_for_mpi()

class Solver:
    def __init__(self,
            env: TaichiEnv,
            model,
            optimizer,
            logger=None,
            cfg=None,
            decay_factor=0.99,
            steps=None,
            **kwargs
        ):
        self.cfg = make_cls_config(self, cfg, **kwargs)
        self.env = env
        self.logger = logger
        self.model = model
        self.optimizer = optimizer
        self.num_substep = env.simulator.substeps
        # For Debug
        self.last_target = None
        self.decay_factor = decay_factor
        self.steps = steps

    @classmethod
    def default_config(cls):
        cfg = CN()
        cfg.optim = Optimizer.default_config()
        cfg.n_iters = 100
        cfg.softness = 666.
        cfg.horizon = 50

        cfg.init_range = 0.
        cfg.init_sampler = 'uniform'
        return cfg

    # For multiple step target only support chamfer and emd loss cannot use default loss
    # Here the state might be problematic since only four frame is considered insided of primitives
    def solve_multistep(
            self, state, actions, targets, localDevice:torch.device
        ) -> Tuple[Union[list, None], Union[torch.Tensor, None], torch.Tensor, Any]:
        """ Run the model on the given state and action`s`. 

        The model will forward the input state for given steps, and
        observe how much it derives from the expected `target` to
        compute the losses and generate the gradient from optimizing.

        The method CAN be executed in a multi-process way.
        
        :param state: a list of states from dataloader
        :param actions: actions to be executed
        :param target: the expected target after the execution, from dataloader as well
        :param local_device: to which CPU/GPU device should the execution be loaded
        :return: a tuple of (resulting states, gradient, loss_first, loss)
        NOTE the first two elements can be None, when there is NAN in the gradient array
        """
        env = self.env
        def forward(state,targets,actions):
            env.set_state(state, self.cfg.softness, False)
            #print("Cursor Before:",env.simulator.cur)
            if self.steps == None:
                steps = len(targets)
            else:
                steps = self.steps if ((self.steps<len(targets))and (self.steps>0)) else len(targets)
            with ti.Tape(env.loss.loss):
                for i in range(steps):
                    env.set_target(targets[i])
                    env.step(actions[i])
                    env.compute_loss(copy_grad=False,decay=self.decay_factor)
                env.set_grad()
            loss = env.loss.loss[None]
            return loss, env.get_state_grad()
        x = torch.from_numpy(state[0]).double().to(localDevice)
        x_hat = self.model(x.float())
        assignment = solve_icp(x_hat, x, 3000)
        x_hat = x_hat[assignment]
        state_hat = copy.deepcopy(state)
        state_hat[0] = x_hat.cpu().double().detach().numpy()
        loss, (x_hat_grad,_) = forward(state_hat,targets,actions)
        x_hat_grad = torch.from_numpy(x_hat_grad).clamp(-1,1).to(localDevice)
        if not torch.isnan(x_hat_grad).any():
            return x_hat, x_hat_grad, 0, loss
        else:
            mpi.msg("NAN Detected")
            return None, None, 0, loss
                    
def _update_network_mpi(model: torch.nn.Module, optimizer, state, gradient, loss, use_loss=True):
    if state is not None and gradient is not None:
        optimizer.zero_grad()
        state.backward(gradient, retain_graph=True)
        if use_loss:
            loss.backward()
    
    if mpi.num_procs()>1: 
        mpi.avg_grads(model)
    if state is not None and gradient is not None:
        optimizer.step()

# Need to create specific dataloader for such task
def _loading_dataset(env_name)->DataLoader:
    """ Load data to memory

    :return: a dataloader of ChopSticksDataset
    """
    if env_name.startswith("Chopsticks"):
        dataset = ChopSticksDataset()
    elif env_name.startswith("Rope"):
        dataset = RopeDataset()
    elif env_name.startswith("Writer"):
        dataset = WriterDataset()
    elif env_name.startswith("Torus"):
        dataset = TorusDataset()

    dataloader = DataLoader(dataset,batch_size = mpi.num_procs())
    return dataloader

def _intialize_env(
    envName: str,
    sdfLoss: float, 
    lossFn:  Union[Type[chamfer_loss.ChamferLoss], Type[emd_loss.EMDLoss], Type[state_loss.StateLoss], Type[loss.Loss]],
    densityLoss: float,
    contactLoss: float,
    srl: bool, 
    softContactLoss: bool,
    seed: int
) -> Tuple[TaichiEnv, int]:
    """ Intialize the environment from the arguments

    The parameters all come from the arguments

    :return: the intialized taichi environment, together with the max episode step of this env
    """
    taichi_env.init_taichi()
    env = make(
        env_name          = envName,
        nn                = False,
        sdf_loss          = sdfLoss,
        loss_fn           = lossFn,
        density_loss      = densityLoss,
        contact_loss      = contactLoss,
        full_obs          = srl,
        soft_contact_loss = softContactLoss
    )
    env.seed(seed)
    env.reset()
    T = env._max_episode_steps
    return env.unwrapped.taichi_env, T

def _intialize_model(taichiEnv: TaichiEnv, device: torch.device,model_name: str)->PCNAutoEncoder:
    """ Intialize the model from a given TaichiEnv onto a certain device

    :param taichiEnv: the environment
    :param device: the device to which the model should be loaded to
    :return: the intialized encoding model
    """
    model = PCNAutoEncoder(taichiEnv.n_particles, HIDDEN_LAYERS, LATENT_DIMS, FEAT_DMIS)
    model.load_state_dict(torch.load(f"pretrain_model/{model_name}.pth"))
    model = model.to(device)
    return model

def squeeze_batch(state):
    state = [state[0].squeeze().numpy(),state[1].squeeze().numpy(),
             state[2].squeeze().numpy(),state[3].squeeze().numpy(),
             state[4].squeeze().numpy()]
    return state

def learn_latent(
        args:Namespace,
        loss_fn:Union[Type[chamfer_loss.ChamferLoss], Type[emd_loss.EMDLoss], Type[state_loss.StateLoss], Type[loss.Loss]]
    ):
    """ Learn latent in the MPI way

    NOTE: neither the Taichi nor the PlasticineEnv shall be
    intialized outside, since the intialization must be
    executed in sub processes. 

    :param args: Arguments passed from the solver.py, determining
        the hyperparameters, the paths and the random seeds. 
    :param loss_fn: the loss function for environment intialization. 
    """
    # before MPI FORK: intialization & data loading
    os.makedirs(args.path, exist_ok=True)
    epochs, batchCnt, batch_size = 5, 0, args.batch_size, 

    # After MPI FORK
    mpi.fork(mpi.best_mpi_subprocess_num(batch_size, procPerGPU=2))
    procLocalDevice = torch.device("cuda:1")

    dataloader = _loading_dataset(args.env_name)
    taichiEnv, T = _intialize_env(args.env_name, args.sdf_loss, loss_fn, args.density_loss,
                                  args.contact_loss, args.srl, args.soft_contact_loss, args.seed)
    model = _intialize_model(taichiEnv, procLocalDevice, args.model_name)
    optimizer = torch.optim.Rprop(model.parameters(), lr=args.lr)
    mpi.msg(f"TaichiEnv Number of Particles:{taichiEnv.n_particles}")

    solver = Solver(
        env       = taichiEnv,
        model     = model,
        optimizer = optimizer,
        logger    = None,
        cfg       = None,
        steps     = args.horizon,
        softness  = args.softness, 
        horizon   = T,
        **{"optim.lr": args.lr, "optim.type":args.optim, "init_range":0.0001}
    )

    procAvgLoss = [0.0] * epochs
    total_batch = 0
    for i in range(epochs):
        batchCnt, efficientBatchCnt = 0, 0
        for stateMiniBatch, targetMiniBatch, actionMiniBatch, indexMiniBatch in dataloader:
            stateProc = list(mpi.batch_collate(
                stateMiniBatch[0], stateMiniBatch[1], stateMiniBatch[2], stateMiniBatch[3], stateMiniBatch[4],
                toNumpy=True
            ))
            targetProc, actionProc, indexProc = mpi.batch_collate(
                targetMiniBatch[0], actionMiniBatch, indexMiniBatch, 
                toNumpy=True
            )
            result_state, gradient, lossInBuffer, currentLoss = solver.solve_multistep(
                state=stateProc,
                actions=actionProc,
                targets=targetProc,
                localDevice = procLocalDevice
            )
            # NOTE Barrier #1
            if result_state is not None and gradient is not None:
                procAvgLoss[i] += currentLoss
                batchLoss = mpi.avg(currentLoss)
                efficientBatchCnt += 1
            else:
                batchLoss = mpi.avg(0, base = 0) # skip this batch loss without ruining the barrier
            # NOTE Barrier #2
            _update_network_mpi(
                model=model,
                optimizer=optimizer,
                state=result_state,
                gradient=gradient,
                loss=lossInBuffer,
                use_loss = False
            )

            if mpi.num_procs()>1: mpi.sync_params(model)

            if mpi.proc_id() == 0:
                mpi.msg(f"Batch:{batchCnt}, loss:{batchLoss}")
                if total_batch == 5 or total_batch==10 or total_batch==1000:
                    torch.save(model.state_dict(),f'pretrain_model/{args.exp_name}_{total_batch}_model.pth')
                    torch.save(model.encoder.state_dict(),f'pretrain_model/{args.exp_name}_{total_batch}_encoder.pth')
            batchCnt += 1
            total_batch += 1
        procAvgLoss[i] /= efficientBatchCnt
        mpi.msg(f"Epoch:{i}, process-local average loss:{procAvgLoss[i]}")

    totalAverageLoss = sum(procAvgLoss) / len(procAvgLoss)
    mpi.msg(f"Total process-local average loss: {totalAverageLoss}")
    totalAverageLoss = mpi.avg(totalAverageLoss)


    if mpi.proc_id() == 0:
        # ONLY one proc can store the model
        mpi.msg(f"Total global average loss:", totalAverageLoss)
        torch.save(model.state_dict(),f"pretrain_model/{args.exp_name}_model.pth")
        torch.save(model.encoder.state_dict(),f"pretrain_model/{args.exp_name}_encoder.pth")
