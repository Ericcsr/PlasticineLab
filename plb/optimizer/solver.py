import taichi as ti
import numpy as np
import copy
import os
from yacs.config import CfgNode as CN

from .optim import Optimizer, Adam, Momentum
from ..engine.taichi_env import TaichiEnv
from ..config.utils import make_cls_config
from ..engine.losses import Loss

OPTIMS = {
    'Adam': Adam,
    'Momentum': Momentum
}


class Solver:
    def __init__(self, env: TaichiEnv, logger=None, cfg=None, **kwargs):
        self.cfg = make_cls_config(self, cfg, **kwargs)
        self.optim_cfg = self.cfg.optim
        self.env = env
        self.logger = logger

    def solve(self, exp_name,init_actions=None, callbacks=()):
        env = self.env
        if init_actions is None:
            init_actions = self.init_actions(env, self.cfg)
        # initialize ...
        optim = OPTIMS[self.optim_cfg.type](init_actions, self.optim_cfg)
        # set softness ..
        env_state = env.get_state() # initial state
        self.total_steps = 0
        self.pc_cnt = 0
        action_buffer = []

        def forward(sim_state, actions):
            if self.logger is not None:
                self.logger.reset()

            env.set_state(sim_state, self.cfg.softness, False) # Set reset the simulator to be initial state
            with ti.Tape(loss=env.loss.loss):
                for i in range(len(actions)):
                    env.step(actions[i])
                    self.total_steps += 1
                    env.compute_loss(taichi_loss=True)
            loss = env.loss.loss[None]
            return loss, env.primitives.get_grad(len(actions))

        def forward_nograd(sim_state,action):
            if self.logger is not None:
                self.logger.reset()

            env.set_state(sim_state, self.cfg.softness,False)
            for i in range(len(action)):
                env.save_current_state(f'raw_data/{exp_name}/state/{self.pc_cnt}')
                action_buffer.append(action[i])
                env.step(action[i])
                self.total_steps += 1
                self.pc_cnt += 1
                env.compute_loss(taichi_loss=True)
            action_buffer.append(np.zeros_like(action[i])) # For alignment
            env.save_current_state(f'raw_data/{exp_name}/state/{self.pc_cnt}')
            self.pc_cnt += 1

        best_action = None
        best_loss = 1e10

        actions = init_actions
        for iter in range(self.cfg.n_iters):
            self.params = actions.copy()
            loss, grads = forward(env_state['state'], actions)
            if loss < best_loss:
                best_loss = loss
                best_action = actions.copy()
            actions = optim.step(grads) # Here we have access to gradient with respect to all actions how about state
            for callback in callbacks:
                callback(self, optim, loss, grads)
            forward_nograd(env_state['state'],actions)
            print("Iteration: ",iter," Loss:",loss)

        env.set_state(**env_state)
        np.save(f'raw_data/{exp_name}/action.npy',action_buffer)
        return best_action

    @staticmethod
    def init_actions(env, cfg):
        action_dim = env.primitives.action_dim
        horizon = cfg.horizon
        if cfg.init_sampler == 'uniform':
            return np.random.uniform(-cfg.init_range, cfg.init_range, size=(horizon, action_dim))
        else:
            raise NotImplementedError

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

def _make_necessary_dirs(path,exp_name):
    os.makedirs(path, exist_ok=True)
    os.makedirs('raw_data',exist_ok=True)
    os.makedirs(f'raw_data/{exp_name}',exist_ok=True)
    os.makedirs(f'raw_data/{exp_name}/state',exist_ok=True)


def solve_action(env, path, logger, args):
    _make_necessary_dirs(path,args.exp_name)
    
    env.reset()
    taichi_env: TaichiEnv = env.unwrapped.taichi_env
    T = env._max_episode_steps
    solver = Solver(taichi_env, logger, None,
                    n_iters=(args.num_steps + T-1)//T, softness=args.softness, horizon=T,
                    **{"optim.lr": args.lr, "optim.type": args.optim, "init_range": 0.0001})

    action = solver.solve(exp_name=args.exp_name)
    print("Done")
