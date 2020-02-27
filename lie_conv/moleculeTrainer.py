import torch
import torch.nn as nn
from oil.model_trainers import Trainer
from lie_conv.lieConv import PointConv, Pass, Swish, GlobalPool
from lie_conv.lieConv import norm, LieResNet, BottleBlock
from lie_conv.utils import export, Named
from lie_conv.datasets import RandomRotation
from lie_conv.lieGroups import SE3
import numpy as np


@export
class MoleculeTrainer(Trainer):
    def __init__(self, *args, task='cv', ds_stats=None, **kwargs):
        super().__init__(*args,**kwargs)
        self.hypers['task'] = task
        self.ds_stats = ds_stats
        if hasattr(self.lr_schedulers[0],'setup_metrics'): #setup lr_plateau if exists
            self.lr_schedulers[0].setup_metrics(self.logger,'valid_MAE')
            
    def loss(self, minibatch):
        y = self.model(minibatch)
        target = minibatch[self.hypers['task']]

        if self.ds_stats is not None:
            median, mad = self.ds_stats
            target = (target - median) / mad

        return (y-target).abs().mean()

    def metrics(self, loader):
        task = self.hypers['task']

        #mse = lambda mb: ((self.model(mb)-mb[task])**2).mean().cpu().data.numpy()
        if self.ds_stats is not None:
            median, mad = self.ds_stats
            def mae(mb):
                target = mb[task]
                y = self.model(mb) * mad + median
                return (y-target).abs().mean().cpu().data.numpy()
        else:
            mae = lambda mb: (self.model(mb)-mb[task]).abs().mean().cpu().data.numpy()
        return {'MAE': self.evalAverageMetrics(loader,mae)}
    
    def logStuff(self,step,minibatch=None):
        super().logStuff(step,minibatch)                            


@export
class MolecResNet(nn.Module,metaclass=Named):
    def __init__(self, num_species, charge_scale=None, ds_frac=1,aug=False, num_outputs=1,
                k=64, nbhd=np.inf, act="swish", bn=True, num_layers=4, 
                mean=False, **kwargs):
        super().__init__()
        conv = lambda k1,k2: PointConv(k1, k2, nbhd=nbhd, ds_frac=ds_frac, bn=bn, 
                                   act=act, mean=mean, xyz_dim=3)
        modules = [RandomRotation() if aug else nn.Sequential(),
            Pass(nn.Linear(3*num_species,k),dim=1), #embedding layer
            *[BottleBlock(k,k,conv,bn=bn,act=act) for _ in range(num_layers)],
            Pass(nn.Linear(k,k//2),dim=1),
            Pass(Swish() if act=='swish' else nn.ReLU(),dim=1),  
            GlobalPool(mean=True),#mean), 
            nn.Linear(k//2,num_outputs)]

        self.net = nn.Sequential(*modules)
        self.charge_scale = charge_scale

    def featurize(self, mb):
        charges = mb['charges'] / self.charge_scale
        c_vec = torch.stack([torch.ones_like(charges),charges,charges**2],dim=-1) # 
        one_hot_charges = (mb['one_hot'][:,:,:,None]*c_vec[:,:,None,:]).float().reshape(*charges.shape,-1)
        atomic_coords = mb['positions'].float()
        atom_mask = mb['charges']>0
        return (atomic_coords, one_hot_charges, atom_mask) 
    def forward(self, mb):
        x = self.featurize(mb)
        return self.net(x).squeeze(-1)
        
@export 
class MolecLieResNet(LieResNet):
    def __init__(self, num_species, charge_scale, aug=False, alpha=.5,group=SE3, **kwargs):
        super().__init__(chin=3*num_species,num_outputs=1,group=group,ds_frac=1,**kwargs)
        self.charge_scale = charge_scale
        self.aug =aug
        self.random_rotate = RandomRotation()
    def featurize(self, mb):
        charges = mb['charges'] / self.charge_scale
        c_vec = torch.stack([torch.ones_like(charges),charges,charges**2],dim=-1) # 
        one_hot_charges = (mb['one_hot'][:,:,:,None]*c_vec[:,:,None,:]).float().reshape(*charges.shape,-1)
        atomic_coords = mb['positions'].float()
        atom_mask = mb['charges']>0
        #print('orig_mask',atom_mask[0].sum())
        return (atomic_coords, one_hot_charges, atom_mask)
    def forward(self,mb):
        with torch.no_grad():
            x = self.featurize(mb)
            x = self.random_rotate(x) if self.aug else x
        return super().forward(x).squeeze(-1)
