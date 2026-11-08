// AMD FidelityFX Super Resolution 3.1.4 upscaler on the frame's DLSS point (runtime/vk_fsr3.c). vk_dlaa.c finds the
// point, makes the motion vectors and jitters the scene exactly as for DLSS, and calls vk_fsr3_evaluate in place of
// NGX's evaluation.
#pragma once
#include "vk.h"

typedef struct {
    VkImageView color;             // the scene (sampled; the light target or the composite)
    VkImageView depth;             // the scene depth, depth aspect
    VkImageView mv;                // RG16F at the render size: last frame's window position minus this one's, pixels
    VkImageView out;               // RGBA16F at the output size, GENERAL: FSR 3 writes the picture here
    uint32_t w, h, ow, oh;         // render and output size
    float jx, jy;                  // this frame's jitter, render pixels (window x right, y down)
    uint32_t phases;               // the jitter sequence's length
    float dt_ms;                   // time since the last evaluation
    float near_z, far_z, fov_y;    // the camera: near and far plane distance, vertical field of view (radians)
    int reset;                     // a new history (cut, size change, gap)
    int hdr;                       // the colour is linear scene light (the light target), else 0..1 (the composite)
} Fsr3Eval;

int vk_fsr3_ready(void);                                      // 1: the pipelines can be made on this device
int vk_fsr3_evaluate(VkCommandBuffer cb, const Fsr3Eval *e);   // records the passes; 0: failed (FSR 3 off)
