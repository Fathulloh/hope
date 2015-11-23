#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import imp
import glob
import time
import ctypes
import argparse
import cv2 as cv
import numpy as np
from multiprocessing import Process, Queue, Array
from chainer import cuda, serializers, Variable


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--model', type=str)
    parser.add_argument('--param', type=str)
    parser.add_argument('--test_dir', type=str)
    parser.add_argument('--sat_size', type=int, default=64)
    parser.add_argument('--map_size', type=int, default=16)
    parser.add_argument('--channels', type=int, default=1)
    parser.add_argument('--offset', type=int, default=1)
    parser.add_argument('--batchsize', type=int, default=128)

    return parser.parse_args()


def create_minibatch(args, ortho, queue):
    minibatch = []
    for d in range(0, args.map_size // 2, (args.map_size // 2) // args.offset):
        for y in range(d, args.h_limit, args.map_size):
            for x in range(d, args.w_limit, args.map_size):
                if ((y + args.sat_size > args.h_limit)
                        or (x + args.sat_size > args.w_limit)):
                    break
                # ortho patch
                o_patch = ortho[
                    y:y + args.sat_size, x:x + args.sat_size, :].astype(
                    np.float32, copy=False)
                o_patch -= o_patch.reshape(-1, 3).mean(axis=0)
                o_patch /= o_patch.reshape(-1, 3).std(axis=0) + 1e-5
                o_patch = o_patch.transpose((2, 0, 1))

                minibatch.append(o_patch)
                if len(minibatch) == args.batchsize:
                    queue.put(np.asarray(minibatch, dtype=np.float32))
                    minibatch = []
    queue.put(None)


def tile_patches(args, canvas, queue):
    for d in range(0, args.map_size // 2, (args.map_size // 2) // args.offset):
        st = time.time()
        for y in range(d, args.h_limit, args.map_size):
            for x in range(d, args.w_limit, args.map_size):
                if ((y + args.sat_size > args.h_limit)
                        or (x + args.sat_size > args.w_limit)):
                    break
                pred = queue.get()
                if pred is None:
                    break
                pred = pred.transpose((1, 2, 0))
                canvas[y:y + args.map_size, x:x + args.map_size, :] += pred
        print('offset:{} ({} sec)'.format(d, time.time() - st))


def get_predict(args, ortho, model):
    xp = cuda.cupy if args.gpu >= 0 else np
    args.h_limit, args.w_limit = ortho.shape[0], ortho.shape[1]
    args.canvas_h = args.h_limit - (args.sat_size - args.map_size)
    args.canvas_w = args.w_limit - (args.sat_size - args.map_size)

    # to share 'canvas' between different threads
    canvas_ = Array(
        ctypes.c_float, args.canvas_h * args.canvas_w * args.channels)
    canvas = np.ctypeslib.as_array(canvas_.get_obj())
    canvas = canvas.reshape((args.canvas_h, args.canvas_w, args.channels))

    # prepare queues and threads
    patch_queue = Queue(maxsize=1)
    preds_queue = Queue()
    patch_worker = Process(
        target=create_minibatch, args=(args, ortho, patch_queue))
    canvas_worker = Process(
        target=tile_patches, args=(args, canvas, preds_queue))
    patch_worker.start()
    canvas_worker.start()

    while True:
        minibatch = patch_queue.get()
        if minibatch is None:
            break
        minibatch = Variable(
            xp.asarray(minibatch, dtype=xp.float32), volatile=True)
        preds = model(minibatch, None).data
        if args.gpu >= 0:
            preds = xp.asnumpy(preds)
        [preds_queue.put(pred) for pred in preds]

    preds_queue.put(None)
    patch_worker.join()
    canvas_worker.join()

    return canvas


if __name__ == '__main__':
    args = get_args()
    model_fn = os.path.basename(args.model)
    model = imp.load_source(model_fn.split('.')[0], args.model).model
    serializers.load_hdf5(args.param, model)
    if args.gpu >= 0:
        cuda.get_device(args.gpu).use()
        model.to_gpu()
    model.train = False

    out_dir = '{}/test'.format(os.path.dirname(args.model))
    if not os.path.exists(out_dir):
        os.mkdir(out_dir)
    for fn in glob.glob('{}/*.tif*'.format(args.test_dir)):
        img = cv.imread(fn)
        pred = get_predict(args, img, model)
        out_fn = '{}/{}.png'.format(
            out_dir, os.path.splitext(os.path.basename(fn))[0])
        print(pred.shape, pred.min(), pred.max())
        cv.imwrite(out_fn, pred * 255)