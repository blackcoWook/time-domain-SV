# -*- coding: utf-8 -*-
"""
@author: Sangwook Han (swhan9873@gm.gist.ac.kr)
"""

# ref : https://github.com/clovaai/voxceleb_trainer/tree/master/models


import torch
import torch.nn as nn
import torch.nn.functional as F

class ResSEBlock(nn.Module):
    expansion = 1
    def __init__(self, inChannels, outChannels, stride=1, downsample=None, reduction=8):
        super(ResSEBlock, self).__init__()
        self.conv1      = nn.Conv1d(inChannels, outChannels, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1        = nn.BatchNorm1d(outChannels)
        
        self.conv2      = nn.Conv1d(outChannels, outChannels, kernel_size=3, padding=1, bias=False)
        self.bn2        = nn.BatchNorm1d(outChannels)
        
        self.relu       = nn.ReLU(inplace=True)
        
        self.se         = SELayer(outChannels, reduction)
        self.downsample = downsample
        self.stride     = stride

    def forward(self, x):
        
        residual = x

        output = self.conv1(x)
        output = self.relu(output)
        output = self.bn1(output)

        output = self.conv2(output)
        output = self.bn2(output)
        output = self.se(output)

        if self.downsample is not None:
            residual = self.downsample(x)

        output += residual
        output = self.relu(output)
        return output
    

class GlobalLayerNorm(nn.Module):
    def __init__(self, dim, eps=1e-05, elementwise_affine=True):
        super(GlobalLayerNorm, self).__init__()
        
        # gln: mean,var N x 1 x 1
        self.dim = dim
        self.eps = eps
        self.elementwise_affine = elementwise_affine

        if self.elementwise_affine:
            self.weight = nn.Parameter(torch.ones(self.dim, 1))
            self.bias = nn.Parameter(torch.zeros(self.dim, 1))
        else:
            self.register_parameter('weight', None)
            self.register_parameter('bias', None)

    def forward(self, x):

        if x.dim() != 3:
            raise RuntimeError("{} accept 3D tensor as input".format(
                self.__name__))
        """
        gNL: the feature is normalized over both the channel and the time dimention (gln: mean,var Batch x 1 x 1)
        
        args:
            x: [batch, channel, n_sample_output (= time )]
        """
        mean    = torch.mean(x, (1, 2), keepdim=True)
        var     = torch.mean((x-mean)**2, (1, 2), keepdim=True)
        
        # N x C x L
        if self.elementwise_affine:
            x = self.weight*(x-mean)/torch.sqrt(var+self.eps)+self.bias
        else:
            x = (x-mean)/torch.sqrt(var+self.eps)
        return x
    
class SELayer(nn.Module):
    def __init__(self, channel, reduction):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Sequential(
                nn.Linear(channel, channel // reduction),
                nn.ReLU(inplace=True),
                nn.Linear(channel // reduction, channel),
                nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1)
        
        return x * y.expand_as(x)
    
class ConvSEBlock(nn.Module):
    
    def __init__(self, in_channels, out_channels, kernel_size = 3,
                 stride = 1, dilation = 1, norm_type = None, causal = False):
        super(ConvSEBlock, self).__init__()
        
        # 128 channels -> 256 channels
        self.conv1x1        = nn.Conv1d(in_channels = in_channels, out_channels = out_channels, kernel_size = 1)
        self.prelu_1        = nn.PReLU()
        self.norm_1         = nn.BatchNorm1d(out_channels)

        
        # Depthwise convolution 
        self.padding        = (kernel_size - 1) * dilation if causal else (kernel_size - 1) * dilation // 2
        self.dwconv         = nn.Conv1d(out_channels, out_channels, kernel_size, 
                                    groups = out_channels, padding = self.padding, dilation = dilation)
        self.prelu_2        = nn.PReLU()
        self.norm_2         = nn.BatchNorm1d(out_channels)

        self.pointwise_conv = nn.Conv1d(in_channels = out_channels, out_channels = in_channels, kernel_size = 1)
        self.se             = SELayer(channel = in_channels,reduction = 16)
        
    def forward(self,x):
        
        identity = x
        
        out = self.conv1x1(x)
        out = self.prelu_1(out)
        out = self.norm_1(out)
        
        out = self.dwconv(out)
        out = self.prelu_2(out)
        out = self.norm_2(out)
    
        out = self.pointwise_conv(out)
        out = self.se(out)
    
        output = identity + out
        
        return F.relu(output) # return output

class FeatureExtraction(nn.Module):
    def __init__(self,H,L,P,M,B,R):
        
        """
        args:
            H: number of channels in encoder (= 512)
            L: kernel length (filter length) (= 40)
            P: number of channels for input/output to ConvSEBlock (= 128)
            M: number of channels in ConvSEBlock (= 256)
            B: number of convolutionals block (= 8)
            R: number of repeats (=3)
        
        """
        
        super(FeatureExtraction,self).__init__()
        
        # ---------------------------- Encoder ------------------------------------ 
        self.encoder        = nn.Conv1d(1,H, kernel_size = L, stride = L//2)
        self.encoder_bn     = nn.BatchNorm1d(H)
        self.encoder_relu   = nn.ReLU()
        # --------------------------- Extractor -----------------------------------
        
        self.botteneck      = nn.Conv1d(in_channels = H, out_channels = P, kernel_size = 1, stride = 1)
        layer = []
        for i in range(R):
            layer.append(self._make_TCN_layer(P,M,B))
            
        self.TCN            = nn.Sequential(*layer)
        self.gNL            = GlobalLayerNorm(P, elementwise_affine = False)
        
    def _make_TCN_layer(self,P,M,B):
        
        layers = []
        for x in range(B):
            layers.append(ConvSEBlock(in_channels = P, out_channels = M, dilation = 2**x))
        return nn.Sequential(*layers)

    def forward(self,x):
        
        x = self.encoder(x)
        x = self.encoder_bn(x)
        x = self.encoder_relu(x)
        
        x        = self.botteneck(x)
        features = self.TCN(x)
        features = self.gNL(features)
        
        return features
        
class HalfResNet34(nn.Module):
    def __init__(self,block,layers,num_filters,speaker_embedding, aggregation = 'SAP'):
        super(HalfResNet34,self).__init__()
    
        self.inplanes       = num_filters[0]
        self.aggregation    = aggregation
        
        self.conv1          = nn.Conv1d(128, num_filters[0] , kernel_size=7, stride=2, padding=3,
                                        bias=False)
        self.bn1            = nn.BatchNorm1d(num_filters[0])
        self.relu           = nn.ReLU(inplace=True)
        self.maxPool        = nn.MaxPool1d(kernel_size=3, stride=2, padding=1)
        
        self.layer1         = self._make_layer(block, num_filters[0], layers[0])
        self.layer2         = self._make_layer(block, num_filters[1], layers[1], stride=2)
        self.layer3         = self._make_layer(block, num_filters[2], layers[2], stride=2)
        self.layer4         = self._make_layer(block, num_filters[3], layers[3], stride=2)
    
        self.avgpool        = nn.AvgPool1d(9, stride = 1)
        
        # utterance level feature extraction
        if self.aggregation == "SAP":
            self.sap_linear     = nn.Linear(num_filters[3] * block.expansion, num_filters[3] * block.expansion)
            self.attention      = self.new_parameter(num_filters[3] * block.expansion, 1)
            out_dim             = num_filters[3] * block.expansion
        else:
            raise ValueError('Undefined encoder')

        self.fc             = nn.Linear(out_dim, speaker_embedding)
        
    def new_parameter(self, *size):
        out = nn.Parameter(torch.FloatTensor(*size))
        nn.init.xavier_normal_(out)
        return out
    
    def _make_layer(self, block, planes, blocks, stride=1):
        
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv1d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm1d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        
        self.inplanes = planes * block.expansion
        for i in range(1, blocks):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)
    
    def forward(self,x):
        
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxPool(x)
        
        x = self.layer1(x)
        x = self.layer2(x)
        x = self.layer3(x)
        x = self.layer4(x)
        x = self.avgpool(x)
        
        if self.aggregation == "SAP":
            x = x.permute(0, 2, 1)
            h = torch.tanh(self.sap_linear(x))
            w = torch.matmul(h, self.attention).squeeze(dim=2)
            w = F.softmax(w, dim=1).view(x.size(0), x.size(1), 1)
            x = torch.sum(x * w, dim=1)
        else:
            raise ValueError('Undefined encoder')

        x = x.view(x.size()[0], -1)
        spk_embedding = self.fc(x)
        
        return spk_embedding

class ConvTasResNet(nn.Module):
    def __init__(self):
        super(ConvTasResNet,self).__init__()
        
        # mean and variance normalization is performed by using instance norm
        self.mvn            = nn.InstanceNorm1d(1)
        self.fe             = FeatureExtraction(H = 512, L = 40, P = 128, M = 256, B = 8, R = 3)
        self.speaker_model  = HalfResNet34(ResSEBlock, layers = [3,4,6,3], num_filters = [32,64,128,256],speaker_embedding = 256)
        
        # init
        for m in self.modules():
            if isinstance(m, nn.Conv1d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, nn.BatchNorm1d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
        
    def forward(self,x):
        
        x                   = self.mvn(x).detach()
        features            = self.fe(x)
        speaker_embedding   = self.speaker_model(features)

        return speaker_embedding


if __name__ == '__main__':
    
    # inputs: [batch, channel, n_samples (= 65536)]
    segments        = torch.randn([4,1,65536])
    model           = ConvTasResNet()
        
    model_params    = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('==> model total parameters: {}'.format(model_params))
    output          = model(segments)
    