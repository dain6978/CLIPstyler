from PIL import Image
import numpy as np

import torch
import torch.nn
import torch.optim as optim
from torchvision import transforms, models
from torchvision.models import VGG19_Weights

import StyleNet
import utils
import clip
import torch.nn.functional as F
import template as T

from PIL import Image 
import PIL 
from torchvision import utils as vutils
import argparse
from torchvision.transforms.functional import adjust_contrast

from basicsr.archs.rrdbnet_arch import RRDBNet
from realesrgan import RealESRGANer

import time


parser = argparse.ArgumentParser()

parser.add_argument('--content_path', type=str, default="./face.jpg",
                    help='Image resolution')
parser.add_argument('--content_name', type=str, default="face",
                    help='Image resolution')
parser.add_argument('--exp_name', type=str, default="exp1",
                    help='Image resolution')
parser.add_argument('--text', type=str, default="Fire",
                    help='Image resolution')
parser.add_argument('--lambda_tv', type=float, default=2e-3,
                    help='total variation loss parameter')
parser.add_argument('--lambda_patch', type=float, default=9000,
                    help='PatchCLIP loss parameter')
parser.add_argument('--lambda_dir', type=float, default=600,
                    help='directional loss parameter')
parser.add_argument('--lambda_c', type=float, default=150,
                    help='content loss parameter')
parser.add_argument('--lambda_act', type=float, default=0,
                    help='activation value parameter')     
parser.add_argument('--crop_size', type=int, default=128,
                    help='cropped image size')
parser.add_argument('--num_crops', type=int, default=64,
                    help='number of patches')
parser.add_argument('--img_width', type=int, default=512,
                    help='size of images')
parser.add_argument('--img_height', type=int, default=512,
                    help='size of images')
parser.add_argument('--max_step', type=int, default=200,
                    help='Number of domains')
parser.add_argument('--lr', type=float, default=5e-4,
                    help='Number of domains')
parser.add_argument('--thresh', type=float, default=0.7,
                    help='Number of domains')
parser.add_argument('--graphic', type=str, default="mid",
                    help='Number of domains')
args = parser.parse_args()

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

assert (args.img_width%8)==0, "width must be multiple of 8"
assert (args.img_height%8)==0, "height must be multiple of 8"

height = args.img_height
width = args.img_width

VGG = models.vgg19(weights=VGG19_Weights.DEFAULT).features
VGG.to(device)

for parameter in VGG.parameters():
    parameter.requires_grad_(False)


upscale_model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64, num_block=23, num_grow_ch=32, scale=4)
upscaler_path = "RealESRGAN_x4plus.pth"

upscaler = RealESRGANer(
    scale=4, 
    model_path=upscaler_path,
    model=upscale_model, 
    tile=0,
    tile_pad=10,
    pre_pad=10
)
upscaler.model.to(device)


def img_denormalize(image):
    mean=torch.tensor([0.485, 0.456, 0.406]).to(device)
    std=torch.tensor([0.229, 0.224, 0.225]).to(device)
    mean = mean.view(1,-1,1,1)
    std = std.view(1,-1,1,1)

    image = image*std +mean
    return image

def img_normalize(image):
    mean=torch.tensor([0.485, 0.456, 0.406]).to(device)
    std=torch.tensor([0.229, 0.224, 0.225]).to(device)
    mean = mean.view(1,-1,1,1)
    std = std.view(1,-1,1,1)

    image = (image-mean)/std
    return image

def clip_normalize(image,device):
    image = F.interpolate(image,size=224,mode='bicubic')
    mean=torch.tensor([0.48145466, 0.4578275, 0.40821073]).to(device)
    std=torch.tensor([0.26862954, 0.26130258, 0.27577711]).to(device)
    mean = mean.view(1,-1,1,1)
    std = std.view(1,-1,1,1)

    image = (image-mean)/std
    return image

    
def get_image_prior_losses(inputs_jit):
    diff1 = inputs_jit[:, :, :, :-1] - inputs_jit[:, :, :, 1:]
    diff2 = inputs_jit[:, :, :-1, :] - inputs_jit[:, :, 1:, :]
    diff3 = inputs_jit[:, :, 1:, :-1] - inputs_jit[:, :, :-1, 1:]
    diff4 = inputs_jit[:, :, :-1, :-1] - inputs_jit[:, :, 1:, 1:]

    loss_var_l2 = torch.norm(diff1) + torch.norm(diff2) + torch.norm(diff3) + torch.norm(diff4)
    
    return loss_var_l2


def compose_text_with_templates(text: str, templates=T.imagenet_templates) -> list:
    return [template.format(text) for template in templates]


def upscale_image(image_tensor):
    #image_tensor: (B, C, H, W), (float, (0~1)) (= ouput tensor)
    #image_np: (H, W, C)
    image_np = image_tensor.detach().squeeze().cpu().numpy()  
    image_np = np.moveaxis(image_np, 0, -1) 
    image_np = (image_np * 255).clip(0, 255).astype(np.uint8)
    
    # upscaler.enhance input & output: uint8, (0~255)
    upscaled_image_np = upscaler.enhance(image_np, outscale=4)[0]
    
    upscaled_tensor = torch.from_numpy(np.moveaxis(upscaled_image_np, -1, 0)).unsqueeze(0).to(device) 
    upscaled_tensor = torch.clamp(upscaled_tensor.float() / 255.0, 0.0, 1.0)
   
    return upscaled_tensor


content_path = args.content_path
content = args.content_name
exp = args.exp_name

content_image = utils.load_image2(content_path, img_height=height,img_width=width)
content_image = content_image.to(device)

content_features = utils.get_features(img_normalize(content_image), VGG)
target = content_image.clone().requires_grad_(True).to(device)

style_net = StyleNet.UNet()
style_net.to(device)

style_weights = {'conv1_1': 0.1,
                 'conv2_1': 0.2,
                 'conv3_1': 0.4,
                 'conv4_1': 0.8,
                 'conv5_1': 1.6}

content_weight = args.lambda_c

show_every = 100
optimizer = optim.Adam(style_net.parameters(), lr=args.lr)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=100, gamma=0.5)
steps = args.max_step

content_loss_epoch = []
style_loss_epoch = []
total_loss_epoch = []

output_image = content_image


default_crop_size = args.crop_size
window_size = 3
activation_average = content_features['conv4_2'][:,:,:,:].mean().item()
acitvation_weight = args.lambda_act

def adjust_crop_size(center_x, center_y):
    y1_img = max(0, center_y - window_size // 2)
    y2_img = min(height, center_y + window_size // 2)
    x1_img = max(0, center_x - window_size // 2)
    x2_img = min(width, center_x + window_size // 2)

    scale_y = content_features['conv4_2'].shape[2] / height
    scale_x = content_features['conv4_2'].shape[3] / width
    
    y1_feat = int(y1_img * scale_y)
    y2_feat = int(y2_img * scale_y) + 1
    x1_feat = int(x1_img * scale_x)
    x2_feat = int(x2_img * scale_x) + 1

    activation_value = content_features['conv4_2'][:, :, y1_feat:y2_feat, x1_feat:x2_feat].mean().item()
    
    threshold = activation_average
    if activation_value > threshold - acitvation_weight:  
        crop_size = default_crop_size // 2  
    else:
        crop_size = default_crop_size
    return crop_size 


def cropper(image, crop_size, center_x, center_y):
    crop_y1 = max(0, center_y - crop_size // 2)
    crop_y2 = min(height, center_y + crop_size // 2)
    crop_x1 = max(0, center_x - crop_size // 2)
    crop_x2 = min(width, center_x + crop_size // 2)

    crop_image = image[:, :, crop_y1:crop_y2, crop_x1:crop_x2]
    return crop_image

augment = transforms.Compose([
    transforms.RandomPerspective(p=1,distortion_scale=0.5),
    transforms.Resize(224)
])


device='cuda'
clip_model, preprocess = clip.load('ViT-B/32', device, jit=False)

style_text = args.text
source_text = "a Photo"

graphic = args.graphic
template_list = T.template_game_mapping.get(graphic)


with torch.no_grad():
    template_style = compose_text_with_templates(style_text, template_list)
    #template_style = compose_text_with_templates(style_text, T.imagenet_templates)
    tokens_style = clip.tokenize(template_style).to(device)
    style_text_features = clip_model.encode_text(tokens_style).detach()
    style_text_features = style_text_features.mean(axis=0, keepdim=True)
    style_text_features /= style_text_features.norm(dim=-1, keepdim=True)
    
    template_source = compose_text_with_templates(source_text, template_list)
    #template_source = compose_text_with_templates(source_text, imagenet_templates)
    #template_source = compose_text_with_templates(source_text, T.imagenet_templates_game)
    tokens_source = clip.tokenize(template_source).to(device)
    source_text_features = clip_model.encode_text(tokens_source).detach()
    source_text_features = source_text_features.mean(axis=0, keepdim=True)
    source_text_features /= source_text_features.norm(dim=-1, keepdim=True)
    
    source_image_features = clip_model.encode_image(clip_normalize(content_image,device))
    source_image_features /= (source_image_features.clone().norm(dim=-1, keepdim=True))

    
num_crops = args.num_crops

start_time = time.time()
total_allocated_memory = 0
total_reserved_memory = 0


for epoch in range(0, steps+1):
    optimizer.zero_grad()

    allocated_memory = torch.cuda.memory_allocated()
    reserved_memory = torch.cuda.memory_reserved()
    total_allocated_memory += allocated_memory
    total_reserved_memory += reserved_memory

    target = style_net(content_image,use_sigmoid=True).to(device)
    target.requires_grad_(True)
    
    target_features = utils.get_features(img_normalize(target), VGG)
    
    content_loss = 0

    content_loss += torch.mean((target_features['conv4_2'] - content_features['conv4_2']) ** 2)
    content_loss += torch.mean((target_features['conv5_2'] - content_features['conv5_2']) ** 2)

    loss_patch=0 
    img_proc =[]
    for n in range(num_crops):
        center_y = np.random.randint(default_crop_size // 2, height - (default_crop_size // 2))
        center_x = np.random.randint(default_crop_size // 2, width - (default_crop_size // 2))

        crop_size = adjust_crop_size(center_x, center_y)
        target_crop = cropper(target, crop_size, center_x, center_y)
        target_crop = augment(target_crop)
        img_proc.append(target_crop)

    img_proc = torch.cat(img_proc,dim=0)
    img_aug = img_proc

    image_features = clip_model.encode_image(clip_normalize(img_aug,device))
    image_features /= (image_features.clone().norm(dim=-1, keepdim=True))

    img_direction = (image_features-source_image_features)
    img_direction /= img_direction.clone().norm(dim=-1, keepdim=True)
    
    text_direction = (style_text_features-source_text_features).repeat(image_features.size(0),1)
    text_direction /= text_direction.norm(dim=-1, keepdim=True)
    loss_temp = (1- torch.cosine_similarity(img_direction, text_direction, dim=1))
    loss_temp[loss_temp<args.thresh] =0
    loss_patch+=loss_temp.mean()
    
    glob_features = clip_model.encode_image(clip_normalize(target,device))
    glob_features /= (glob_features.clone().norm(dim=-1, keepdim=True))
    
    glob_direction = (glob_features-source_image_features)
    glob_direction /= glob_direction.clone().norm(dim=-1, keepdim=True)
    
    loss_glob = (1- torch.cosine_similarity(glob_direction, text_direction, dim=1)).mean()
    
    reg_tv = args.lambda_tv*get_image_prior_losses(target)

    total_loss = args.lambda_patch*loss_patch + content_weight * content_loss+ reg_tv+ args.lambda_dir*loss_glob
    total_loss_epoch.append(total_loss)

    optimizer.zero_grad()
    total_loss.backward()
    optimizer.step()
    scheduler.step()

    if epoch % 20 == 0:
        print("After %d criterions:" % epoch)
        print('Total loss: ', total_loss.item())
        print('Content loss: ', content_loss.item())
        print('patch loss: ', loss_patch.item())
        print('dir loss: ', loss_glob.item())
        print('TV loss: ', reg_tv.item())
    
    if epoch % 50 == 0:
        out_path1 = './outputs/'+style_text+'_'+content+'_'+exp+'.jpg'
        out_path2 = './outputs/'+style_text+'_'+content+'_'+exp+'4x.jpg'
        output_image = target.clone()
        output_image = torch.clamp(output_image,0,1)
        output_image = adjust_contrast(output_image,1.5)
        vutils.save_image(
                                    output_image,
                                    out_path1,
                                    nrow=1,
                                    normalize=True)
        if (epoch == steps):
            output_image = upscale_image(output_image)
            vutils.save_image(
                                    output_image,
                                    out_path2,
                                    nrow=1,
                                    normalize=True)


end_time = time.time()
total_time = end_time - start_time 
print(f'Total execution Time: {end_time - start_time:.2f} seconds')

final_allocated_memory = torch.cuda.memory_allocated()
final_reserved_memory = torch.cuda.memory_reserved()

print(f"Total Allocated Memory: {total_allocated_memory / (1024 ** 3):.2f} GB")
print(f"Total Reserved Memory: {total_reserved_memory / (1024 ** 3):.2f} GB")