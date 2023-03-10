# Copyright (C) 2022. Huawei Technologies Co., Ltd. All rights reserved.
# This program is free software; you can redistribute it and/or modify
# it under the terms of the MIT License.
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# MIT License for more details.

import os
import numpy as np
from tqdm import tqdm
from argparse import ArgumentParser

import torch
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

import params
from data import VCDecDataset, VCDecBatchCollate
from model.vc import DiffVC
from model.utils import FastGL
from utils import save_plot, save_audio

n_mels = params.n_mels
sampling_rate = params.sampling_rate
n_fft = params.n_fft
hop_size = params.hop_size

channels = params.channels
filters = params.filters
layers = params.layers
kernel = params.kernel
dropout = params.dropout
heads = params.heads
window_size = params.window_size
enc_dim = params.enc_dim

dec_dim = params.dec_dim
spk_dim = params.spk_dim
use_ref_t = params.use_ref_t
beta_min = params.beta_min
beta_max = params.beta_max

random_seed = params.seed
test_size = params.test_size

val_file = 'filelists/valid.txt'
exc_file = 'filelists/exceptions.txt'

log_dir = 'logs_dec'
enc_dir = 'logs_enc'
learning_rate = 1e-4


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument('--local_rank', type=int) # For DDP
    parser.add_argument('-d', '--data-dir', required=True, help='The directory the datasets located in.')
    parser.add_argument('-p', '--decoder-path', help='The path to the decoder model saved file.')
    parser.add_argument('-e', '--epochs', type=int, default=100, help='How many epochs will the training process go through.')
    parser.add_argument('--epoch-start', type=int, default=1, help='The number epoch counting will start from.')
    parser.add_argument('-b', '--batch-size', type=int, default=16, help='The batch size.')
    parser.add_argument('-s', '--save-every', type=int, default=1, help='Save the decoder every n epochs.')
    args = parser.parse_args()
    data_dir = args.data_dir
    decoder_path = args.decoder_path
    epochs = args.epochs
    epoch_start = args.epoch_start
    batch_size = args.batch_size
    save_every = args.save_every
        

    torch.manual_seed(random_seed)
    np.random.seed(random_seed)

    os.makedirs(log_dir, exist_ok=True)

    print('Setting GPU devices...')
    local_rank = args.local_rank
    torch.distributed.init_process_group(backend='gloo') # backend='gloo' for Windows
    torch.cuda.set_device(local_rank)
    device = torch.device('cuda', local_rank)

    print('Initializing data loaders...')
    train_set = VCDecDataset(data_dir, val_file, exc_file)
    collate_fn = VCDecBatchCollate()
    train_sampler = DistributedSampler(train_set)
    train_loader = DataLoader(train_set, batch_size=batch_size, sampler=train_sampler,
                              collate_fn=collate_fn, num_workers=4, drop_last=True)

    print('Initializing and loading models...')
    fgl = FastGL(n_mels, sampling_rate, n_fft, hop_size).cuda()
    model = DiffVC(n_mels, channels, filters, heads, layers, kernel, 
                   dropout, window_size, enc_dim, spk_dim, use_ref_t, 
                   dec_dim, beta_min, beta_max)
    if decoder_path is not None:
        checkpt = torch.load(decoder_path, map_location='cpu')
        model.load_state_dict(checkpt['state_dict'])
    model.load_encoder(
        './conformer_ppg_model/en_conformer_ctc_att/config.yaml', 
        './conformer_ppg_model/en_conformer_ctc_att/24epoch.pth',
        'cpu'
    )
    print(f'Number of parameters: {model.nparams}')

    # print('Encoder:')
    # print(model.encoder)
    # print('Number of parameters = %.2fm\n' % (model.encoder.nparams/1e6))
    # print('Decoder:')
    # print(model.decoder)
    # print('Number of parameters = %.2fm\n' % (model.decoder.nparams/1e6))

    print('Enabling multi-GPU training...')
    device_count = torch.cuda.device_count()
    print(f'Number of GPU devices: {device_count}')
    model.to(device)
    model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank],
                                                      output_device=local_rank,
                                                      find_unused_parameters=True)

    print('Initializing optimizers...')
    optimizer = torch.optim.Adam(params=model.module.decoder.parameters(), lr=learning_rate)
    if decoder_path is not None:
        optimizer.load_state_dict(checkpt['optim'])

    print('Start training.')
    torch.backends.cudnn.benchmark = True
    iteration = 0
    for epoch in range(epoch_start, epochs + epoch_start):
        print(f'Epoch: {epoch} [iteration: {iteration}]')
        train_loader.sampler.set_epoch(epoch) # Shuffle data
        model.train()
        losses = []
        for batch in tqdm(train_loader, total=len(train_set)//batch_size):
            wav = batch['wav'].cuda()
            mel, mel_ref = batch['mel1'].cuda(), batch['mel2'].cuda()
            c, mel_lengths = batch['c'].cuda(), batch['mel_lengths'].cuda()
            model.zero_grad()
            loss = model.module.compute_loss(wav, mel, mel_lengths, mel_ref, c)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.module.decoder.parameters(), max_norm=1)
            optimizer.step()
            losses.append(loss.item())
            iteration += 1

        losses = np.asarray(losses)
        msg = 'Epoch %d: loss = %.4f\n' % (epoch, np.mean(losses))
        print(msg)
        with open(f'{log_dir}/train_dec.log', 'a') as f:
            f.write(msg)
        losses = []

        if local_rank == 0: # Eval on the cuda:0 GPU
            if epoch % save_every > 0:
                continue

            model.eval()
            print('Inference...\n')
            with torch.no_grad():
                mels = train_set.get_valid_dataset()
                for i, (wav, mel, c) in enumerate(mels):
                    if i >= test_size:
                        break
                    wav = wav.unsqueeze(0).float().cuda()
                    mel = mel.unsqueeze(0).float().cuda()
                    c = c.unsqueeze(0).float().cuda()
                    mel_lengths = torch.LongTensor([mel.shape[-1]]).cuda()
                    ppg, mel_rec = model(wav, mel, mel_lengths, mel, mel_lengths, c, n_timesteps=100)
                    if epoch == save_every:
                        save_plot(mel.squeeze().cpu(), f'{log_dir}/original/original_{i}.png')
                        save_plot(ppg.squeeze().cpu(), f'{log_dir}/ppg/ppg_{i}.png')
                        audio = fgl(mel)
                        save_audio(f'{log_dir}/original/original_{i}.wav', sampling_rate, audio)
                    save_plot(mel_rec.squeeze().cpu(), f'{log_dir}/reconstructed/reconstructed_{i}_e{epoch}.png')
                    audio = fgl(mel_rec)
                    save_audio(f'{log_dir}/reconstructed/reconstructed_{i}_e{epoch}.wav', sampling_rate, audio)

            print('Saving model...\n')
            encoder_exclude = model.module.encoder
            model.module.encoder = None
            ckpt = {
                'state_dict': model.module.state_dict(), 
                'optim': optimizer.state_dict(),
            }
            torch.save(ckpt, f=f"{log_dir}/vc_{epoch}.pt")
            model.module.encoder = encoder_exclude
