#!/usr/bin/env python

# Packages import
import multiprocessing
import os
import time

import numpy as np
import pandas as pd

from typing import Any, Optional, Tuple

import torch
import torch.nn as nn
import torchvision
import torchvision.transforms as transforms
import torch.backends.cudnn as cudnn

from torch.utils.data import TensorDataset, DataLoader, Dataset
from sklearn.preprocessing import LabelEncoder
from PIL import Image
from tqdm import tqdm

# Global variables
IN_KERNEL = os.environ.get('KAGGLE_WORKING_DIR') is not None
MIN_SAMPLES_PER_CLASS = 50
BATCH_SIZE = 512 # depends on the card memory and the images resolution
LEARNING_RATE = 2e-4 # already used [5e-5, 1e-4, 3e-4, 4e-4]
LR_STEP = 3 
LR_FACTOR = 0.33 # for the Adam optimizer
NUM_WORKERS = multiprocessing.cpu_count() # for the parallel inference
MAX_STEPS_PER_EPOCH = 15000
NUM_EPOCHS = 2 ** 32

LOG_FREQ = 500
NUM_TOP_PREDICTS = 20
TIME_LIMIT = 9 * 60 * 60 # used for training into the Kaggle kernels

#The dataset preparation
class ImageDataset(torch.utils.data.Dataset):
    '''
    This class consists of three main functions:
    '''
    def __init__(self, dataframe: pd.DataFrame, mode: str) -> None:
        print(f'creating data loader - {mode}')
        assert mode in ['train', 'val', 'test']

        self.df = dataframe
        self.mode = mode
        
        transforms_list = []

        if self.mode == 'train':
            transforms_list = [
                transforms.RandomHorizontalFlip(p = 0.2),
                transforms.RandomChoice([
                    transforms.RandomResizedCrop(64),
                    transforms.ColorJitter(0.2, 0.2, 0.2, 0.2),
                    transforms.RandomAffine(degrees=8, translate=(0.2, 0.2),
                                            scale=(0.75, 1.25), shear=15,
                                            resample=Image.BILINEAR)
                ])
            ]
            
        transforms_list.extend([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                  std=[0.229, 0.224, 0.225]),
        ])
        self.transforms = transforms.Compose(transforms_list)
        
    def __getitem__(self, index: int) -> Any:
        ''' Returns: tuple (sample, target) '''
        filename = self.df.id.values[index]
        
    
        if self.mode == 'test':
            sample = Image.open(f'../input/googlelandmarks201964x64part3/test2/test2/{filename}.jpg')
            assert sample.mode == 'RGB'
        elif self.mode == 'train' and filename[0] in '01234567':
            part = 1
            directory = 'train_' + filename[0]
            sample = Image.open(f'../input/google-landmarks-2019-64x64-part{part}/{directory}/{self.mode}_64/{filename}.jpg')
            assert sample.mode == 'RGB'
        else:
            part = 2
            directory = 'train_' + filename[0]
            sample = Image.open(f'../input/google-landmarks-2019-64x64-part{part}/{directory}/{self.mode}_64/{filename}.jpg')
            assert sample.mode == 'RGB'

        image = self.transforms(sample)

        if self.mode == 'test':
            return image
        else:
            return image, self.df.landmark_id.values[index]

    def __len__(self) -> int:
        return self.df.shape[0]

def GAP(predicts: torch.Tensor, confs: torch.Tensor, targets: torch.Tensor) -> float:
    ''' Implementation of the GAP metric '''
    
    assert len(predicts.shape) == 1
    assert len(confs.shape) == 1
    assert len(targets.shape) == 1
    assert predicts.shape == confs.shape and confs.shape == targets.shape

    _, indices = torch.sort(confs, descending=True)

    confs = confs.cpu().numpy()
    predicts = predicts[indices].cpu().numpy()
    targets = targets[indices].cpu().numpy()

    res, true_pos = 0.0, 0

    for i, (c, p, t) in enumerate(zip(confs, predicts, targets)):
        rel = int(p == t)
        true_pos += rel

        res += true_pos / (i + 1) * rel

    res /= targets.shape[0] 
    return res

class AverageMeter:
    ''' Computes and stores the average and current value '''
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.val = 0.0
        self.avg = 0.0
        self.sum = 0.0
        self.count = 0

    def update(self, val: float, n: int = 1) -> None:
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

def load_data() -> 'Tuple[DataLoader[np.ndarray], DataLoader[np.ndarray], LabelEncoder, int]':
    torch.multiprocessing.set_sharing_strategy('file_system')
    cudnn.benchmark = True

    # Include in the training process the classes with at least MIN_SAMPLES_PER_CLASS samples
    print('loading data...')
    df = pd.read_csv('../input/google-landmarks-2019-64x64-part1/train.csv')
    df.drop(columns='url', inplace=True)

    counts = df.landmark_id.value_counts()
    selected_classes = counts[counts >= MIN_SAMPLES_PER_CLASS].index
    num_classes = selected_classes.shape[0]
    print('classes with at least N samples:', num_classes)

    train_df = df.loc[df.landmark_id.isin(selected_classes)].copy()
    print('train_df', train_df.shape)

    test_df = pd.read_csv('../input/landmarksupportfiles/test2.csv', dtype=str)
    test_df.drop(columns='url', inplace=True)
    print('test_df', test_df.shape)

    label_encoder = LabelEncoder()
    label_encoder.fit(train_df.landmark_id.values)
    print('found classes', len(label_encoder.classes_))
    assert len(label_encoder.classes_) == num_classes

    train_df.landmark_id = label_encoder.transform(train_df.landmark_id)

    train_dataset = ImageDataset(train_df, mode='train')
    test_dataset = ImageDataset(test_df, mode='test')
    
    
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE,
                              shuffle=False, num_workers=NUM_WORKERS, drop_last=True)

    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE,
                             shuffle=False, num_workers=NUM_WORKERS)

    return train_loader, test_loader, label_encoder, num_classes

def train(train_loader: Any, model: Any, criterion: Any, optimizer: Any,
          epoch: int, lr_scheduler: Any) -> None:
    print(f'epoch {epoch}')
    batch_time = AverageMeter()
    losses = AverageMeter()
    avg_score = AverageMeter()

    model.train()
    num_steps = min(len(train_loader), MAX_STEPS_PER_EPOCH)

    print(f'total batches: {num_steps}')

    end = time.time()
    lr_str = ''

    for i, (input_, target) in enumerate(train_loader):
        if i >= num_steps:
            break

        output = model(input_.cuda())
        loss = criterion(output, target.cuda())

        confs, predicts = torch.max(output.detach(), dim=1)
        avg_score.update(GAP(predicts, confs, target))

        losses.update(loss.data.item(), input_.size(0))
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        batch_time.update(time.time() - end)
        end = time.time()

        if i % LOG_FREQ == 0:
            print(f'{epoch} [{i}/{num_steps}]\t'
                        f'time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        f'loss {losses.val:.4f} ({losses.avg:.4f})\t'
                        f'GAP {avg_score.val:.4f} ({avg_score.avg:.4f})'
                        + lr_str)

        if has_time_run_out():
            break

    print(f' * average GAP on train {avg_score.avg:.4f}')

def inference(data_loader: Any, model: Any) -> Tuple[torch.Tensor, torch.Tensor,
                                                     Optional[torch.Tensor]]:
    ''' Returns predictions and targets, if any. '''
    model.eval()

    activation = nn.Softmax(dim=1)
    all_predicts, all_confs, all_targets = [], [], []
    
    with torch.no_grad():
        for i, data in enumerate(tqdm(data_loader, disable=IN_KERNEL)):
            if data_loader.dataset.mode != 'test':
                input_, target = data
            else:
                input_, target = data, None

            output = model(input_.cuda())
            output = activation(output)

            confs, predicts = torch.topk(output, NUM_TOP_PREDICTS)
            all_confs.append(confs)
            all_predicts.append(predicts)

            if target is not None:
                all_targets.append(target)

    predicts = torch.cat(all_predicts)
    confs = torch.cat(all_confs)
    targets = torch.cat(all_targets) if len(all_targets) else None

    return predicts, confs, targets

def generate_submission(test_loader: Any, model: Any, label_encoder: Any) -> np.ndarray:
    sample_sub = pd.read_csv('../input/landmarksupportfiles/recognition_sample_submission2.csv')

    predicts_gpu, confs_gpu, _ = inference(test_loader, model)
    predicts, confs = predicts_gpu.cpu().numpy(), confs_gpu.cpu().numpy()

    labels = [label_encoder.inverse_transform(pred) for pred in predicts]
    print('labels')
    print(np.array(labels))
    print('confs')
    print(np.array(confs))

    sub = test_loader.dataset.df
    def concat(label: np.ndarray, conf: np.ndarray) -> str:
        return ' '.join([f'{L} {c}' for L, c in zip(label, conf)])
    sub['landmarks'] = [concat(label, conf) for label, conf in zip(labels, confs)]

    sample_sub = sample_sub.set_index('id')
    sub = sub.set_index('id')
    sample_sub.update(sub)

    sample_sub.to_csv('submission.csv')

def has_time_run_out() -> bool:
    return time.time() - global_start_time > TIME_LIMIT - 800

if __name__ == '__main__':
    global_start_time = time.time()
    train_loader, test_loader, label_encoder, num_classes = load_data()

    model = torchvision.models.resnet50(pretrained=True)
    model.avg_pool = nn.AdaptiveAvgPool2d(1)
    model.fc = nn.Linear(model.fc.in_features, num_classes)
    model.cuda()

    criterion = nn.CrossEntropyLoss()

    optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=LR_STEP,
                                                   gamma=LR_FACTOR)

    for epoch in range(1, NUM_EPOCHS + 1):
        print('-' * 50)
        train(train_loader, model, criterion, optimizer, epoch, lr_scheduler)
        lr_scheduler.step()

        if has_time_run_out():
            break

    print('inference mode')
    generate_submission(test_loader, model, label_encoder)
