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
from ..engine.losses import state_loss, emd_loss, chamfer_loss, loss
from ..engine.taichi_env import TaichiEnv
from ..envs import make
from ..neurals.autoencoder import PCNAutoEncoder
from ..neurals.pcdataloader import ChopSticksDataset,RopeDataset

mpi.setup_pytorch_for_mpi()

HIDDEN_LAYERS = 256
LATENT_DIMS   = 1024
FEAT_DMIS     = 3

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
        loss_first,assignment = compute_emd(x, x_hat, 3000)
        x_hat_after = x_hat[assignment.detach().long()]
        x_hat = x_hat_after
        state_hat = copy.deepcopy(state)
        state_hat[0] = x_hat.cpu().double().detach().numpy()
        loss, (x_hat_grad,_) = forward(state_hat,targets,actions)
        x_hat_grad = torch.from_numpy(x_hat_grad).clamp(-1,1).to(localDevice)
        if not torch.isnan(x_hat_grad).any():
            return x_hat, x_hat_grad, loss_first, loss
        else:
            mpi.msg("NAN Detected")
            return None, None, loss_first, loss

    # No grad version
    def exec_multistep(
            self, state, actions, targets, localDevice:torch.device
        ) -> Tuple[Union[list, None], Union[torch.Tensor, None], torch.Tensor, Any]:
        """ Run the model on the given state and action`s`. 

        The model will forward the input state for given steps, and
        observe how much it derives from the expected `target` to
        compute the losses.

        The method CAN be executed in a multi-process way.
        
        :param state: a list of states from dataloader
        :param actions: actions to be executed
        :param target: the expected target after the execution, from dataloader as well
        :param local_device: to which CPU/GPU device should the execution be loaded
        :return: a tuple of (resulting states, gradient, loss_first, loss)
        """
        env = self.env
        def forward(state,targets,actions):
            env.set_state(state, self.cfg.softness, False)
            if self.steps == None:
                steps = len(targets)
            else:
                steps = self.steps if ((self.steps<len(targets))and (self.steps>0)) else len(targets)
            for i in range(steps):
                env.set_target(targets[i])
                env.step(actions[i])
                env.compute_loss(copy_grad=False,decay=self.decay_factor)
            loss = env.loss.loss[None]
            return loss
        x = torch.from_numpy(state[0]).double().to(localDevice)
        x_hat = self.model(x.float())
        loss_first,assignment = compute_emd(x, x_hat, 3000)
        x_hat_after = x_hat[assignment.detach().long()]
        x_hat = x_hat_after
        state_hat = copy.deepcopy(state)
        state_hat[0] = x_hat.cpu().double().detach().numpy()
        loss = forward(state_hat,targets,actions)
        return loss_first, loss


                    
def _update_network_mpi(model: torch.nn.Module, optimizer, state, gradient, loss, use_loss=True):
    if state is not None and gradient is not None:
        optimizer.zero_grad()
        state.backward(gradient, retain_graph=True)
        if use_loss:
            loss.backward()
    mpi.avg_grads(model)
    if state is not None and gradient is not None:
        optimizer.step()

# Need to create specific dataloader for such task
def _loading_dataset()->DataLoader:
    """ Load data to memory

    :return: a dataloader of ChopSticksDataset
    """
    #dataset = ChopSticksDataset()
    dataset = RopeDataset()
    dataloader = DataLoader(dataset,batch_size = mpi.num_procs())
    return dataloader, dataset

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

def _intialize_model(taichiEnv: TaichiEnv, device: torch.device)->PCNAutoEncoder:
    """ Intialize the model from a given TaichiEnv onto a certain device

    :param taichiEnv: the environment
    :param device: the device to which the model should be loaded to
    :return: the intialized encoding model
    """
    model = PCNAutoEncoder(taichiEnv.n_particles, HIDDEN_LAYERS, LATENT_DIMS, FEAT_DMIS)
    model.load_state_dict(torch.load("pretrain_model/network_emd_finetune_rope.pth")['net_state_dict'])
    #torch.save(model.encoder.state_dict(),'pretrain_model/emd_expert_encoder.pth')
    model = model.to(device)
    return model

def _update_loss(index,loss,dataset):
    data = mpi.gather_loss_id(index,loss)
    if data != None:
        idxs, losses = data
        dataset.recordLoss(idxs,losses)


def _update_focal_scheme(dataset,size=1000):
    mpi.sync_loss(dataset.loss_table)
    subdataset = dataset.getSubset(size)
    dataloader = DataLoader(subdataset,batch_size=mpi.num_procs())
    return dataloader

def learn_latent_focal(
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
    epochs, batch_cnt, batch_size = 20, 0, args.batch_size, 

    # After MPI FORK
    mpi.fork(mpi.best_mpi_subprocess_num(batch_size, procPerGPU=2))
    procLocalDevice = torch.device("cuda")

    dataloader,original_dataset = _loading_dataset()
    taichiEnv, T = _intialize_env(args.env_name, args.sdf_loss, loss_fn, args.density_loss,
                                  args.contact_loss, args.srl, args.soft_contact_loss, args.seed)
    model = _intialize_model(taichiEnv, procLocalDevice)
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
    use_grad = [False]
    procAvgLoss = [0.0] * epochs
    for i in range(epochs):
        batchCnt = 0
        efficientBatchCnt = 0
        for stateMiniBatch, targetMiniBatch, actionMiniBatch,indexMiniBatch in dataloader:
            stateProc = list(mpi.batch_collate(
                stateMiniBatch[0], stateMiniBatch[1], stateMiniBatch[2], stateMiniBatch[3], stateMiniBatch[4],
                toNumpy=True
            ))
            targetProc, actionProc, indexProc = mpi.batch_collate(
                targetMiniBatch[0], actionMiniBatch, indexMiniBatch,
                toNumpy=True
            )
            if use_grad[0]:
                result_state, gradient, lossInBuffer, currentLoss = solver.solve_multistep(
                    state=stateProc,
                    actions=actionProc,
                    targets=targetProc,
                    localDevice = procLocalDevice
                )
                if result_state is not None and gradient is not None:
                    procAvgLoss[i] += currentLoss
                    batchLoss = mpi.avg(currentLoss)
                    efficientBatchCnt += 1
                else:
                    batchLoss = mpi.avg(0,base=0)

                _update_network_mpi(
                    model=model,
                    optimizer=optimizer,
                    state=result_state,
                    gradient=gradient,
                    loss=lossInBuffer,
                    use_loss=False
                )
                if mpi.num_procs() > 1: mpi.sync_params(model)
            else:
                lossInBuffer, currentLoss = solver.exec_multistep(
                    state=stateProc,
                    actions=actionProc,
                    targets=targetProc,
                    localDevice=procLocalDevice)
                _update_loss(indexProc,currentLoss,original_dataset)
                procAvgLoss[i] += currentLoss
                batchLoss = mpi.avg(currentLoss)
                efficientBatchCnt += 1                
            if mpi.proc_id() == 0:
                mpi.msg(f"Batch:{batchCnt}, loss:{batchLoss}")
            batchCnt += 1
        procAvgLoss[i] /= efficientBatchCnt
        mpi.msg(f"Epoch:{i}, process-local average loss:{procAvgLoss[i]}")
        if i % 5==0:
            

            dataloader = _update_focal_scheme(original_dataset,1000)
            use_grad[0] = True
        elif i%5 == 4:
            use_grad[0] = False


    totalAverageLoss = sum(procAvgLoss) / len(procAvgLoss)
    mpi.msg(f"Total process-local average loss: {totalAverageLoss}")
    totalAverageLoss = mpi.avg(totalAverageLoss)

    if mpi.proc_id() == 0:
        # ONLY one proc can store the model
        torch.save(model.state_dict(),"pretrain_model/focal_rope.pth")
        torch.save(model.encoder.state_dict(),"pretrain_model/focal_rope_encoder.pth")
