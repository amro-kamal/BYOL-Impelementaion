import torch
import torch.nn as nn
import torch.nn.functional as F
import pytorch_lightning as pl
import math
# This for kNN prediction is from here:
# https://github.com/IgorSusmelj/barlowtwins/blob/main/utils.py

def knn_predict(feature, feature_bank, feature_labels, classes: int, knn_k: int, knn_t: float):
    """Helper method to run kNN predictions on features based on a feature bank
    Args:
        feature: Tensor of shape [N, D] consisting of N D-dimensional features
        feature_bank: Tensor of a database of features used for kNN
        feature_labels: Labels for the features in our feature_bank
        classes: Number of classes (e.g. 10 for CIFAR-10)
        knn_k: Number of k neighbors used for kNN
        knn_t: 
    """
    # compute cos similarity between each feature vector and feature bank ---> [B, N]
    sim_matrix = torch.mm(feature, feature_bank)
    # [B, K]
    sim_weight, sim_indices = sim_matrix.topk(k=knn_k, dim=-1)
    # [B, K]
    sim_labels = torch.gather(feature_labels.expand(feature.size(0), -1), dim=-1, index=sim_indices)
    # we do a reweighting of the similarities 
    sim_weight = (sim_weight / knn_t).exp()
    # counts for each class
    one_hot_label = torch.zeros(feature.size(0) * knn_k, classes, device=sim_labels.device)
    # [B*K, C]
    one_hot_label = one_hot_label.scatter(dim=-1, index=sim_labels.view(-1, 1), value=1.0)
    # weighted score ---> [B, C]
    pred_scores = torch.sum(one_hot_label.view(feature.size(0), -1, classes) * sim_weight.unsqueeze(dim=-1), dim=1)
    pred_labels = pred_scores.argsort(dim=-1, descending=True)
    return pred_labels


class BenchmarkModule(pl.LightningModule):
    """A PyTorch Lightning Module for automated kNN callback
    
    At the end of every training epoch we create a feature bank by inferencing
    the backbone on the dataloader passed to the module. 
    At every validation step we predict features on the validation data.
    After all predictions on validation data (validation_epoch_end) we evaluate
    the predictions on a kNN classifier on the validation data using the 
    feature_bank features from the train data.
    We can access the highest accuracy during a kNN prediction using the 
    max_accuracy attribute.
    """
    def __init__(self, dataloader_kNN, gpus, classes, knn_k, knn_t):
        super().__init__()
        self.resnet = nn.Module()
        self.max_accuracy = 0.0
        self.dataloader_kNN = dataloader_kNN
        self.gpus = gpus
        self.classes = classes
        self.knn_k = knn_k
        self.knn_t = knn_t

    def training_step(self, batch, batch_idx):
        # training_step defines the train loop. It is independent of forward
        (x1,x2), y = batch
        z1 , z2  = self.online_resnet(x1), self.target_resnet(x2) # projections, n-by-d
        p1  = self.predictor(z1) # predictions, n-by-d

        loss= self.mse_loss(p1, z2) # loss
        #TODO:update the target network
        for target_params, online_params in zip(self.target_resnet.parameters(), self.online_resnet.parameters()):
            target_params = target_params * self.taw + (1-self.taw) * online_params
        #TODO: update taw
        self.taw=1- (1-self.taw) * (math.cos(math.pi*(self.step/self.total_steps)) + 1)/2
        self.step+=1
        print(f'step {step}')

        self.log('train_loss', loss)
        return loss

    def training_epoch_end(self, outputs):
        # update feature bank at the end of each training epoch
        self.resnet.eval()
        self.feature_bank = []
        self.targets_bank = []
        with torch.no_grad():
            for data in self.dataloader_kNN:
                # img, target, _ = data
                img, target = data
                if self.gpus > 0:
                    img = img.cuda()
                    target = target.cuda()
                feature = self.resnet(img).squeeze()
                feature = F.normalize(feature, dim=1)
                self.feature_bank.append(feature)
                self.targets_bank.append(target)
        self.feature_bank = torch.cat(self.feature_bank, dim=0).t().contiguous()
        self.targets_bank = torch.cat(self.targets_bank, dim=0).t().contiguous()
        self.resnet.train()

    def validation_step(self, batch, batch_idx):
        # we can only do kNN predictions once we have a feature bank
        if hasattr(self, 'feature_bank') and hasattr(self, 'targets_bank'):
            # images, targets, _ = batch
            images, targets = batch
            feature = self.resnet(images).squeeze()
            feature = F.normalize(feature, dim=1)
            pred_labels = knn_predict(feature, self.feature_bank, self.targets_bank, self.classes, self.knn_k, self.knn_t)
            num = images.size(0)
            top1 = (pred_labels[:, 0] == targets).float().sum().item()
            return (num, top1)
    
    def validation_epoch_end(self, outputs):
        if outputs:
            total_num = 0
            total_top1 = 0.
            for (num, top1) in outputs:
                total_num += num
                total_top1 += top1
            acc = float(total_top1 / total_num)
            if acc > self.max_accuracy:
                self.max_accuracy = acc
            self.log('kNN_accuracy', acc * 100.0, prog_bar=True)
