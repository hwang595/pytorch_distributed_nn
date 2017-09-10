import sys
import math
import threading
import argparse

import torch
from torch.autograd import Variable
from torch._utils import _flatten_tensors, _unflatten_tensors
from torch.cuda.comm import broadcast_coalesced
from torch.cuda import nccl
import torch.distributed as dist

import torch.nn as nn
from distributed_functions.distributed_backward import backward
from torch.nn.parallel.replicate import replicate
from torch.nn.parallel.scatter_gather import scatter_kwargs, gather
from torch.nn.parallel.parallel_apply import parallel_apply
import torch.nn.functional as F

from torchvision import datasets, transforms

'''this is a trial example, we use MNIST on LeNet for simple test here'''

def printgradnorm(self, grad_input, grad_output):
    print('Inside ' + self.__class__.__name__ + ' backward')
    print('Inside class:' + self.__class__.__name__)
    print('')
    
    print('grad_input: ', type(grad_input))
    print('grad_input[0]: ', type(grad_input[0]))
    print('grad_output: ', type(grad_output))
    print('grad_output[0]: ', type(grad_output[0]))
    
    print('')
    if not isinstance(grad_input[0], type(None)):
        print('grad_input size:', grad_input[0].size())
        print('grad_input norm:', grad_input[0].data.norm())
    print('grad_output size:', grad_output[0].size())
    #print('grad_output size:', grad_output)


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

def add_fit_args(parser):
    """
    parser : argparse.ArgumentParser
    return a parser added with args required by fit
    """
    # Training settings
    parser.add_argument('--batch-size', type=int, default=512, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--test-batch-size', type=int, default=1000, metavar='N',
                        help='input batch size for testing (default: 1000)')
    parser.add_argument('--epochs', type=int, default=10, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                        help='SGD momentum (default: 0.5)')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='disables CUDA training')
    parser.add_argument('--seed', type=int, default=1, metavar='S',
                        help='random seed (default: 1)')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status')
    args = parser.parse_args()
    return args

# communication functions come in here:
def asynchronous_fetch_weights():
	''' Fetch all layer weights asynchronously. (from master) '''
	pass


def synchronous_fetch_step():
	''''synchronously fetch global step from master'''
	pass


def asynchronous_fetch_step_update():
	'''asynchronously fetch model from master'''
	pass


def asynchronous_fetch_step():
	'''synchronously fetch global step from master'''
	pass

# we use LeNet here for our simple case
class LeNet(nn.Module):
    def __init__(self):
        super(LeNet, self).__init__()
        self.conv1 = nn.Conv2d(1, 20, 5, 1)
        self.conv2 = nn.Conv2d(20, 50, 5, 1)
        self.fc1 = nn.Linear(4*4*50, 500)
        self.fc2 = nn.Linear(500, 10)
        self.ceriation = nn.CrossEntropyLoss()
    def forward(self, x, target):
        x = self.conv1(x)
        x = F.max_pool2d(x, 2, 2)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.max_pool2d(x, 2, 2)
        x = F.relu(x)
        x = x.view(-1, 4*4*50)
        x = self.fc1(x)
        x = self.fc2(x)
        loss = self.ceriation(x, target)
        return x, loss
    def name(self):
        return 'lenet'

class DistributedWorker:
    def __init__(self, rank, world_size, args):
        self._step_changed = False
        self._update_step = False
        self._new_step_queued = 0
        self._rank = rank
        self._world_size = world_size
        self._cur_step = 0
        self._next_step = self._cur_step + 1
        self._step_fetch_request = False
        self.max_num_epochs = args.epochs
        self.lr = args.lr
        self.momentum = args.momentum

    def build_model(self):
        self.network = LeNet()

        # only for test use
        self.module = self.network

        # this is only used for test
        self.optimizer = torch.optim.SGD(self.network.parameters(), lr=self.lr, momentum=self.momentum)
        self.network.conv1.register_backward_hook(printgradnorm)
        self.network.conv2.register_backward_hook(printgradnorm)
        self.network.fc1.register_backward_hook(printgradnorm)
        self.network.fc2.register_backward_hook(printgradnorm)

    def test_model(self):
        '''this is only for test, please don't call this function'''
        from copy import deepcopy
        self._module_copies = [deepcopy(self.module)]
        self.device_ids = []

        t = None
        for p in self.module.parameters():
            tp = type(p.data)
            if t is not None and t is not tp:
                raise ValueError("DistributedDataParallel requires all parameters' data to be of the same type")
            t = tp

        self.bucket_sizes = []
        self.bucket_map = {}
        MB = 1024 * 1024
        self.broadcast_bucket_size = 10 * MB  # used for param sync before forward
        bucket_bytes_cap = 1 * MB
        bucket_bytes = bucket_bytes_cap  # to init the first bucket immediately
        for param_tuple in zip(*map(lambda m: m.parameters(), self._module_copies)):
            if bucket_bytes >= bucket_bytes_cap:
                self.bucket_sizes.append(0)
                bucket_bytes = 0
            self.bucket_sizes[-1] += 1
            for p in param_tuple:
                self.bucket_map[p] = len(self.bucket_sizes) - 1
            bucket_bytes += p.numel() * p.element_size()

        self.buckets = [[[] for _ in range(len(self.device_ids))] for _ in range(len(self.bucket_sizes))]
        self.bucket_events = [[None] * len(self.device_ids) for _ in range(len(self.bucket_sizes))]
        self.reduced = [False] * len(self.bucket_sizes)

    def train(self, train_loader=None):
        self.network.train()

        # iterate of epochs
        for i in range(self.max_num_epochs):            
            for batch_idx, (data, y_batch) in enumerate(train_loader):
                data, target = Variable(data), Variable(y_batch)
                self.optimizer.zero_grad()
                logits, loss = self.network(data, target)
                loss.backward()
                #backward(loss)

                self.optimizer.step()
                # calculate training accuracy
                prec1, prec5 = accuracy(logits.data, y_batch, topk=(1, 5))
                # load the training info
                print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\tPrec@1: {}\tPrec@5: {}'.format(
                    i, batch_idx * len(data), len(train_loader.dataset),
                    100. * batch_idx / len(train_loader), loss.data[0], 
                    prec1.numpy()[0], 
                    prec5.numpy()[0]))


if __name__ == "__main__":
    args = add_fit_args(argparse.ArgumentParser(description='PyTorch MNIST Example'))

    # load training and test set here:
    train_loader = torch.utils.data.DataLoader(
    datasets.MNIST('../data', train=True, download=True,
                   transform=transforms.Compose([
                       transforms.ToTensor(),
                       transforms.Normalize((0.1307,), (0.3081,))
                   ])), batch_size=args.batch_size, shuffle=True)

    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('../data', train=False, transform=transforms.Compose([
                           transforms.ToTensor(),
                           transforms.Normalize((0.1307,), (0.3081,))
                       ])), batch_size=args.test_batch_size, shuffle=True)

    dist_worker = DistributedWorker(rank=0, world_size=1, args=args)
    dist_worker.build_model()
    dist_worker.train(train_loader=train_loader)