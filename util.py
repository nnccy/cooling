import numpy as np
import os
import pandas as pd
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
from sklearn.metrics import ConfusionMatrixDisplay, confusion_matrix
plt.switch_backend('agg')

import time

class SimpleLogger(object):
    def __init__(self, f, header='#logger output'):
        dir = os.path.dirname(f)
        #print('test dir', dir, 'from', f)
        if not os.path.exists(dir):
            os.makedirs(dir)
        with open(f, 'w') as fID:
            fID.write('%s\n'%header)
        self.f = f

    def __call__(self, *args):
        #standard output
        print(*args)
        #log to file
        try:
            with open(self.f, 'a') as fID:
                fID.write(' '.join(str(a) for a in args)+'\n')
        except:
            print('Warning: could not log to', self.f)


def show_data(t, target, pred, folder, tag, msg=''):
    length = min(t.shape[0], target.shape[0], pred.shape[0])
    t, target, pred = [x[-length:] for x in [t, target, pred]]

    plt.clf()
    plt.figure(1)
    maxv = np.max(target)
    minv = np.min(target)
    view = maxv - minv

    # linear
    n = target.shape[1]
    for i in range(n):
        ax_i = plt.subplot(n, 1, i+1)
        plt.plot(t, target[:, i], 'g--')
        plt.plot(t, pred[:, i], 'r.')
        #ax_i.set_ylim(minv - view/10, maxv + view/10)
        if i == 0:
            plt.title(msg)

    #fig, axs = plt.subplots(6, 1)
    #for i, ax in enumerate(axs):
    #    ax.plot(target[:, i], 'g--', pred[:, i], 'r-')

    plt.savefig("%s/%s.png"%(folder, tag))
    plt.close('all')


def init_weights(m):
    classname = m.__class__.__name__
    if classname.find('Linear') != -1:
        nn.init.kaiming_normal_(m.weight)




def array_operate_with_nan(array, operator):
    assert len(array.shape) == 2
    means = []
    for i in range(array.shape[1]):
        temp_col = array[:, i]
        means.append(operator(temp_col[temp_col == temp_col]))
    return np.array(means, dtype=np.float32)


class TimeRecorder:
    def __init__(self):
        self.infos = {}

    def __call__(self, info, *args, **kwargs):
        class Context:
            def __init__(self, recoder, info):
                self.recoder = recoder
                self.begin_time = None
                self.info = info

            def __enter__(self):
                self.begin_time = time.time()

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.recoder.infos[self.info] = time.time() - self.begin_time

        return Context(self, info)

    def __str__(self):
        return ' '.join(['{}:{:.2f}s'.format(info, t) for info, t in self.infos.items()])


def interpolate_tensors_with_nan(tensors):
    raise NotImplementedError
    tr = TimeRecorder()
    from common.interpolate import NaturalCubicSpline, natural_cubic_spline_coeffs
    with tr('linspace'):
        truth_time_steps = torch.linspace(0, 1, tensors.shape[1]).to(tensors.device)
    with tr('cubic spline'):
        coeffs = natural_cubic_spline_coeffs(truth_time_steps, tensors)
        interpolation = NaturalCubicSpline(truth_time_steps, coeffs)
    with tr('interpolation'):
        tensors_nonan = torch.stack([interpolation.evaluate(t) for t in truth_time_steps], dim=1)
    print(tr)
    return tensors_nonan


def add_state_label(df):
    def is_nan(x):
        return x != x

    pcooling, power_cooling, Ti = df['Pcooling'], df['Power cooling'], df['Ti']
    states = []
    cur_state = 0
    for item, (cooling, power, ti) in enumerate(zip(pcooling, power_cooling, Ti)):
        nxt_i = min(item + 1, len(df) - 1)
        nc, np, nti = pcooling[nxt_i], power_cooling[nxt_i], Ti[nxt_i]
        if is_nan(cooling) or is_nan(power):
            states.append(cur_state)
            continue
        if cur_state == 0:
            if 0 <= cooling <= 0:
                cur_state = 1
            elif cooling == 23300:
                cur_state = 4
        elif cur_state == 1:
            if ti >= 20:
                cur_state = 2
        elif cur_state == 2:
            if power > np and power > 5000:
                cur_state = 3
        elif cur_state == 3:
            if cooling == 23300:
                cur_state = 4
        elif cur_state == 4:
            if cooling <= 17000 and ti <= 13:
                cur_state = 1
        states.append(cur_state)
    ndf = df.copy(deep=True)
    ndf['states'] = states
    return ndf


def get_Dataset(path):
    df = pd.read_csv(path)
    df = process_dataset(df)
    return df[['Pserver', 'Tr']], df[['Ti', 'Pcooling', 'Power cooling']], df[['time']], df[['states']]


def process_dataset(df):

    df = add_state_label(df)
    from datetime import datetime
    beg_time_str = df['Time'].iloc[0]
    beg_time = datetime.strptime(beg_time_str[:-3]+beg_time_str[-2:], '%Y-%m-%dT%H:%M:%S%z')
    df['time'] = df['Time'].apply(
        lambda time_str: (datetime.strptime(time_str[:-3]+time_str[-2:], '%Y-%m-%dT%H:%M:%S%z')-beg_time
                          ).total_seconds()/10
    )
    df['delta'] = df['time'][1:] - df['time'][:-1]
    df.interpolate(axis=0, method='linear', limit_direction='both', inplace=True)
    return df


def get_mlp_network(layer_sizes, outputs_size):

    modules_list = []
    for i in range(1, len(layer_sizes)):
        modules_list.append(
            nn.Linear(layer_sizes[i - 1], layer_sizes[i])
        )
        modules_list.append(nn.Tanh())
    modules_list.append(
        nn.Linear(layer_sizes[-1], outputs_size)
    )
    return nn.Sequential(*modules_list)


def visualize_prediction(Y_label, Y_pred, s_test, base_dir, seg_length=500, dir_name='visualizations'):
    assert len(Y_pred) == len(Y_label)
    if not os.path.exists(os.path.join(base_dir, dir_name)):
        os.mkdir(os.path.join(base_dir, dir_name))
    max_state = int(np.max(s_test))
    ID = 0
    for begin in range(0, len(Y_pred), seg_length):
        ID += 1
        plt.figure(figsize=(15, 12))
        y_label_seg = Y_label[begin:min(begin + seg_length, len(Y_label))]
        y_pred_seg = Y_pred[begin:min(begin + seg_length, len(Y_pred))]
        s_test_seg = s_test[begin:min(begin + seg_length, len(Y_pred))]
        #         scatter = plt.scatter(np.arange(begin, begin+len(tdf)), tdf['Power cooling'], c=tdf['states'], s=10)
        X = np.arange(begin, begin + len(y_label_seg))
        outputs_names = ['Ti', 'Pcooling', 'Power cooling']
        classes = ['unknown', 'closed', 'start-1', 'start-2', 'cooling']
        for i, y_name in enumerate(outputs_names):
            plt.subplot(3, 1, i + 1)
            y_label = y_label_seg[:, i]
            y_pred = y_pred_seg[:, i]
            plt.plot(X, y_label, '-k', label='Time Series')
            for state in range(max_state+1):
                indices = (s_test_seg.squeeze(axis=-1) == state)
                scatter = plt.scatter(X[indices], y_pred[indices], label='pred-'+classes[state], s=5, marker='o')
            plt.xlabel('indexes')
            plt.ylabel(y_name)
            plt.legend()

        plt.savefig(os.path.join(
            base_dir, dir_name, '%i-%i-%i.png' % (ID, begin, begin + seg_length)
        ))
        plt.close()


def display_states_confusion_matrix(true, pred, path, labels, print_handle=print):

    true_label = list(map(lambda x: labels[x], true))
    pred_label = list(map(lambda x: labels[x], pred))
    final_labels = [labels[x] for x in set(true)]
    cm = confusion_matrix(
        true_label,
        pred_label,
        labels=final_labels
    )
    print_handle('Confusion matrix: \n', cm)
    disp = ConfusionMatrixDisplay(cm, display_labels=final_labels)
    disp.plot(cmap='Greens')
    plt.savefig('%s.png' % path)


def t2np(tensor):
    return tensor.squeeze(dim=0).detach().cpu().numpy()
