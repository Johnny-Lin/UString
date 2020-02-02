from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import numpy as np
import pickle
import torch
from torch.utils.data import Dataset
import networkx
import itertools


class DADDataset(Dataset):
    def __init__(self, data_path, phase='training', toTensor=False, device='cuda', vis=False):
        self.data_path = data_path
        self.phase = phase
        self.toTensor = toTensor
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.vis = vis

        filepath = os.path.join(data_path, phase)
        self.files_list = self.get_filelist(filepath)

    def __len__(self):
        data_len = len(self.files_list)
        return data_len

    def get_filelist(self, filepath):
        assert os.path.exists(filepath)
        file_list = []
        for filename in sorted(os.listdir(filepath)):
            file_list.append(filename)
        return file_list

    def __getitem__(self, index):
        data_file = os.path.join(self.data_path, self.phase, self.files_list[index])
        assert os.path.exists(data_file)
        try:
            data = np.load(data_file)
            features = data['data']  # 100 x 20 x 4096
            labels = data['labels']  # 2
            detections = data['det']  # 100 x 19 x 6
        except:
            raise IOError('Load data error! File: %s'%(data_file))
        
        graph_edges, edge_weights = generate_st_graph(detections)

        if self.toTensor:
            features = torch.Tensor(features).to(self.device)         #  100 x 20 x 4096
            labels = torch.Tensor(labels).to(self.device)
            graph_edges = torch.Tensor(graph_edges).long().to(self.device)
            edge_weights = torch.Tensor(edge_weights).to(self.device)

        if self.vis:
            toa = 90
            video_id = str(data['ID'])[5:11]  # e.g.: b001_000490_*
            return features, labels, graph_edges, edge_weights, toa, detections, video_id
        else:
            return features, labels, graph_edges, edge_weights


class A3DDataset(Dataset):
    def __init__(self, data_path, phase='train', toTensor=False, device='cuda', vis=False):
        self.data_path = data_path  # VGRNN/data/a3d/
        self.phase = phase
        self.toTensor = toTensor
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.vis = vis

        self.files_list, self.labels_list = self.read_datalist(data_path, phase)

    def __len__(self):
        data_len = len(self.files_list)
        return data_len

    def read_datalist(self, data_path, phase):
        # load training set
        list_file = os.path.join(data_path, 'features', '%s.txt' % (phase))
        fid = open(list_file, 'r')
        data_files, data_labels = [], []
        for line in fid.readlines():
            filename, label = line.rstrip().split(' ')
            data_files.append(filename)
            data_labels.append(int(label))
        fid.close()

        return data_files, data_labels

    def get_toa(self, clip_id):
        label_file = os.path.join(self.data_path, 'frame_labels', clip_id + '.txt')
        assert os.path.exists(label_file)
        f = open(label_file, 'r')
        label_all = []
        for line in f.readlines():
            label = int(line.rstrip().split(' ')[1])
            label_all.append(label)
        f.close()
        label_all = np.array(label_all, dtype=np.int32)
        toa = np.where(label_all == 1)[0][0]
        return toa

    def __getitem__(self, index):
        data_file = os.path.join(self.data_path, 'features', self.files_list[index])
        assert os.path.exists(data_file)
        data = np.load(data_file)
        features = data['features']
        label = self.labels_list[index]
        label_onehot = np.array([0, 1]) if label > 0 else np.array([1, 0])

        # construct graph
        file_id = self.files_list[index].split('/')[1].split('.npz')[0]
        attr = 'positive' if label > 0 else 'negative'
        dets_file = os.path.join(self.data_path, 'detections', attr, file_id + '.pkl')
        assert os.path.exists(dets_file)
        with open(dets_file, 'rb') as f:
            detections = pickle.load(f)
            detections = np.array(detections)  # 100 x 19 x 6
            graph_edges, edge_weights = generate_st_graph(detections)
        f.close()

        if self.toTensor:
            features = torch.Tensor(features).to(self.device)          #  100 x 20 x 4096
            label_onehot = torch.Tensor(label_onehot).to(self.device)  #  2
            graph_edges = torch.Tensor(graph_edges).long().to(self.device)
            edge_weights = torch.Tensor(edge_weights).to(self.device)

        if self.vis:
            toa = self.get_toa(file_id)
            video_path = os.path.join(self.data_path, 'video_frames', file_id, 'images')
            assert os.path.exists(video_path)
            return features, label_onehot, graph_edges, edge_weights, toa, detections, video_path
        else:
            return features, label_onehot, graph_edges, edge_weights


def generate_st_graph(detections):
    # create graph edges
    num_frames, num_boxes = detections.shape[:2]
    num_edges = int(num_boxes * (num_boxes - 1) / 2)
    graph_edges = []
    edge_weights = np.zeros((num_frames, num_edges), dtype=np.float32)
    for i in range(num_frames):
        # generate graph edges (fully-connected)
        edge = generate_graph_from_list(range(num_boxes))
        graph_edges.append(np.transpose(np.stack(edge).astype(np.int32)))  # 2 x 171
        # compute the edge weights by distance
        edge_weights[i] = compute_graph_edge_weights(detections[i, :, :4], edge)  # 171,

    return graph_edges, edge_weights

       
def generate_graph_from_list(L, create_using=None):
   G = networkx.empty_graph(len(L),create_using)
   if len(L)>1:
       if G.is_directed():
            edges = itertools.permutations(L,2)
       else:
            edges = itertools.combinations(L,2)
       G.add_edges_from(edges)
   graph_edges = list(G.edges())

   return graph_edges


def compute_graph_edge_weights(boxes, edges):
    """
    :param: boxes: (19, 4)
    :param: edges: (171, 2)
    :return: weights: (171,)
    """
    N = boxes.shape[0]
    assert len(edges) == N * (N-1) / 2
    weights = np.ones((len(edges),), dtype=np.float32)
    for i, edge in enumerate(edges):
        c1 = [0.5 * (boxes[edge[0], 0] + boxes[edge[0], 2]),
              0.5 * (boxes[edge[0], 1] + boxes[edge[0], 3])]
        c2 = [0.5 * (boxes[edge[1], 0] + boxes[edge[1], 2]),
              0.5 * (boxes[edge[1], 1] + boxes[edge[1], 3])]
        d = (c1[0] - c2[0])**2 + (c1[1] - c2[1])**2
        weights[i] = np.exp(-d)
    # normalize weights
    if np.sum(weights) > 0:
        weights = weights / np.sum(weights)  # N*(N-1)/2,
    else:
        weights = np.ones((len(edges),), dtype=np.float32)

    return weights


if __name__ == '__main__':
    epoch = 2
    batch_size = 16
    from torch.utils.data import DataLoader

#    # test A3D dataset
#    data_path = '../data/a3d'
#    train_data = A3DDataset(data_path, 'train', toTensor=True, device='gpu')
#    a3d_loader = DataLoader(dataset=train_data, batch_size=batch_size, shuffle=False)
#    for e in range(epoch):
#        print('Epoch: %d'%(e))
#        b = 1
#        for features, labels, graph_edges, edge_weights in a3d_loader:
#            print('--------batch: %d--------'%(b))
#            print('feature dim:', features.size())
#            print('label dim:', labels.size())
#            print('graph edges dim:', graph_edges.size())
#            print('edge weights dim:', edge_weights.size())
#            b += 1


    # test DAD dataset
    data_path = '../data/dad'
    train_data = DADDataset(data_path, 'training', toTensor=True, device='cuda')
    dad_loader = DataLoader(dataset=train_data, batch_size=batch_size, shuffle=False)
    for e in range(epoch):
        print('Epoch: %d'%(e))
        for i, (features, labels, graph_edges, edge_weights) in enumerate(dad_loader):
            print('--------batch: %d--------'%(i))
            print('feature dim:', features.size(), 'device:', features.device)
            print('label dim:', labels.shape, 'device:', labels.device)
            print('graph edges dim:', graph_edges.size(), 'device:', graph_edges.device, 'dtype: ', graph_edges.dtype)
            print('edge weights dim:', edge_weights.size(), 'device:', edge_weights.device)

