import torch
import numpy as np
from PIL import Image, ImageEnhance, ImageFilter # 新增 ImageFilter 用于优化背景
from diffusers import StableDiffusionXLInpaintPipeline
from transformers import pipeline
from plyfile import PlyData, PlyElement
import uvicorn
from fastapi import FastAPI, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
import io
from fastapi.responses import FileResponse
import warnings

# 【总裁级优化】: 屏蔽无害的 Diffusers 配置文件警告，保持控制台整洁
warnings.filterwarnings("ignore", message=".*config attributes.*")
warnings.filterwarnings("ignore", category=FutureWarning)

app = FastAPI(title="OmniSplat AI Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class OmniSplatPipeline:
    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.dtype = torch.float16
        print("[Engine] Initializing World Model & Depth Estimator...")
        
    def generate_panorama(self, init_image: Image.Image, prompt: str) -> Image.Image:
        print("[Engine] Loading SDXL Model...")
        pipe = StableDiffusionXLInpaintPipeline.from_pretrained(
            "diffusers/stable-diffusion-xl-1.0-inpainting-0.1",
            torch_dtype=self.dtype,
            variant="fp16",
            use_safetensors=True
        )
        
        pipe.enable_model_cpu_offload()
        pipe.enable_vae_slicing()

        pano_w, pano_h = 1024, 512
        
        # 【修复3：全景图补全优化】
        # 抛弃纯黑背景。将原图拉伸并进行强力高斯模糊作为底图，为SDXL提供完美的色彩和结构参考，彻底解决“一半黑”问题。
        bg_context = init_image.resize((pano_w, pano_h)).filter(ImageFilter.GaussianBlur(radius=50))
        canvas = bg_context.copy()
        
        offset = ((pano_w - init_image.width) // 2, (pano_h - init_image.height) // 2)
        canvas.paste(init_image, offset)
        
        mask = Image.new("L", (pano_w, pano_h), 255)
        mask.paste(Image.new("L", init_image.size, 0), offset)

        print("[Engine] Generating 360 Panorama (This may take a moment)...")
        with torch.inference_mode():
            pano_img = pipe(
                prompt=prompt + ", 360 degree equirectangular panorama, highly detailed, 8k",
                image=canvas,
                mask_image=mask,
                num_inference_steps=20, 
                strength=0.99
            ).images[0]
        
        del pipe
        torch.cuda.empty_cache()
        return pano_img

    def estimate_depth(self, image: Image.Image) -> np.ndarray:
        print("[Engine] Estimating Depth with DepthAnything...")
        depth_pipe = pipeline(task="depth-estimation", model="LiheYoung/depth-anything-small-hf", device=0 if self.device=="cuda" else -1)
        
        with torch.inference_mode():
            depth_output = depth_pipe(image)
            
        depth_map = np.array(depth_output["depth"])
        
        del depth_pipe
        torch.cuda.empty_cache()
        return depth_map

    def create_gaussian_ply(self, rgb: Image.Image, depth: np.ndarray, output_path: str):
        print("[Engine] Compiling 3D Gaussian Splats...")
        
        # 【总裁级色彩优化】: 提升 30% 的色彩饱和度，确保点云在 3D 空间中色彩惊艳
        enhancer = ImageEnhance.Color(rgb)
        rgb_enhanced = enhancer.enhance(1.3) 
        
        rgb_np = np.array(rgb_enhanced) / 255.0
        h, w = depth.shape
        
        phi = np.linspace(0, np.pi, h)
        theta = np.linspace(0, 2 * np.pi, w)
        theta, phi = np.meshgrid(theta, phi)
        
        r = 10.0 / (depth / 255.0 + 0.1) 
        
        x = r * np.sin(phi) * np.cos(theta)
        y = r * np.cos(phi)
        z = r * np.sin(phi) * np.sin(theta)
        
        points = np.stack((x, y, z), axis=-1).reshape(-1, 3)
        colors = rgb_np.reshape(-1, 3)
        
        num_pts = points.shape[0]
        scales = np.full((num_pts, 3), -4.0)
        rots = np.zeros((num_pts, 4))
        rots[:, 0] = 1.0 
        opacities = np.full((num_pts, 1), 2.0) 
        
        sh_C0 = 0.28209479177387814
        f_dc = (colors - 0.5) / sh_C0
        
        # 【数据结构优化】: 增加 red, green, blue 字段，完美兼容 Three.js PLYLoader
        dtype = [('x', 'f4'), ('y', 'f4'), ('z', 'f4'),
                 ('nx', 'f4'), ('ny', 'f4'), ('nz', 'f4'),
                 ('f_dc_0', 'f4'), ('f_dc_1', 'f4'), ('f_dc_2', 'f4'),
                 ('opacity', 'f4'),
                 ('scale_0', 'f4'), ('scale_1', 'f4'), ('scale_2', 'f4'),
                 ('rot_0', 'f4'), ('rot_1', 'f4'), ('rot_2', 'f4'), ('rot_3', 'f4'),
                 ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')]
        
        elements = np.empty(num_pts, dtype=dtype)
        elements['x'], elements['y'], elements['z'] = points[:,0], points[:,1], points[:,2]
        elements['nx'], elements['ny'], elements['nz'] = np.zeros(num_pts), np.zeros(num_pts), np.zeros(num_pts)
        elements['f_dc_0'], elements['f_dc_1'], elements['f_dc_2'] = f_dc[:,0], f_dc[:,1], f_dc[:,2]
        elements['opacity'] = opacities[:,0]
        elements['scale_0'], elements['scale_1'], elements['scale_2'] = scales[:,0], scales[:,1], scales[:,2]
        elements['rot_0'], elements['rot_1'], elements['rot_2'], elements['rot_3'] = rots[:,0], rots[:,1], rots[:,2], rots[:,3]
        
        # 写入标准 RGB 颜色 (0-255)
        elements['red'] = np.clip(colors[:,0] * 255, 0, 255).astype(np.uint8)
        elements['green'] = np.clip(colors[:,1] * 255, 0, 255).astype(np.uint8)
        elements['blue'] = np.clip(colors[:,2] * 255, 0, 255).astype(np.uint8)
        
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(output_path)
        print(f"[Engine] Gaussian Splat saved to {output_path}")

pipeline_instance = OmniSplatPipeline()

@app.post("/generate")
async def generate(image: UploadFile, prompt: str = Form(...)):
    img_bytes = await image.read()
    init_image = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((512, 512))
    
    pano_img = pipeline_instance.generate_panorama(init_image, prompt)
    depth_map = pipeline_instance.estimate_depth(pano_img)
    output_file = "output_splat.ply"
    pipeline_instance.create_gaussian_ply(pano_img, depth_map, output_file)
    
    return FileResponse(output_file, media_type="application/octet-stream", filename="output_splat.ply")

# 【修复4：新增直传全景图接口】
@app.post("/generate_direct")
async def generate_direct(image: UploadFile):
    img_bytes = await image.read()
    # 直接读取并调整为全景图标准尺寸，跳过SDXL补全
    pano_img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((1024, 512))
    
    depth_map = pipeline_instance.estimate_depth(pano_img)
    output_file = "output_splat_direct.ply"
    pipeline_instance.create_gaussian_ply(pano_img, depth_map, output_file)
    
    return FileResponse(output_file, media_type="application/octet-stream", filename="output_splat_direct.ply")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)