// AMD FidelityFX Super Resolution 3.1.4 upscaler (temporal), our own Vulkan host for AMD's GLSL passes.
//
// The passes are AMD's, unmodified (runtime/shaders/ffx3, from the FidelityFX SDK v1.1.4's Vulkan back end; MIT,
// ffx3/LICENSE.txt), compiled by tools/build_shaders.py into runtime/fsr3_ldr_spv.h and fsr3_hdr_spv.h with the options
// listed there. This file does what the SDK's ffx_fsr3upscaler.cpp (fsr3upscalerCreate, fsr3upscalerDispatch) and its
// Vulkan back end do, without either: it makes the internal images, fills the constant blocks and records the passes in AMD's order:
//   prepare inputs (dilated motion and depth, the previous frame's nearest depth, luma), luma pyramid (SPD: scene
//   luminance and the farthest depth's half-size mip), shading change pyramid and shading change, prepare reactivity
//   (disocclusion, locks, the accumulation), luma instability, accumulate (the upscale into the output).
// Not used: the reactive and transparency masks (AMD's 1x1 zero defaults bound instead), the RCAS pass and the
// accumulate pass's sharpening (vk_dlaa.c runs DLSS with sharpness 0 as well), the LUT Lanczos (the reference one is
// compiled in, so the LUT binding gets a 1x1 placeholder), half precision and the debug view.
//
// Inputs (vk_dlaa.c evaluate): motion vectors at the render size in pixels, last frame's position minus this one's,
// without the jitter; the jitter in render pixels, x right and y down (the same values DLSS gets; FSR's convention is
// the same: the shift of the picture the offset caused); depth near 0 to far 1 (Astral Chain tests LEQUAL); the camera's
// near and far plane and vertical field of view from its projection, which FSR uses to turn depth into view distance
// for its disocclusion test.
//
// Each image is kept in GENERAL. Every pass is followed by a full memory barrier (the passes are small; AMD's back end
// places one per resource transition). The constant blocks come from a host-visible ring of SLOTS frames. One context
// (images and frame index) per render and output size; another size makes it again after an idle wait.
#include <math.h>
#include <string.h>

#include "host.h"
#include "vk.h"
#include "vk_fsr3.h"
#include "vk_rec.h"
#include "fsr3_hdr_spv.h"
#include "fsr3_ldr_spv.h"
#include "platform/platform.h"

enum { P_PREPARE_INPUTS, P_LUMA_PYRAMID, P_SHADING_PYRAMID, P_SHADING_CHANGE, P_PREPARE_REACTIVITY, P_LUMA_INSTABILITY,
       P_ACCUMULATE, P_N };
enum { S, U, C };   // a binding's descriptor: sampled image, storage image, uniform buffer
typedef struct { uint8_t n, type[13]; } PassLayout;
// Each pass's bindings 0..n-1, as its ffx_fsr3upscaler_*_pass.glsl numbers them (FSR3UPSCALER_BIND_*); bindings 1000
// and 1001 are the point and linear clamp samplers of ffx_fsr3upscaler_callbacks_glsl.h, pushed with the rest (as
// immutable samplers in a push descriptor layout, the Khronos validation layer crashed at the first dispatch).
static const PassLayout LAYOUT[P_N] = {
    {9, {S, S, S, U, U, U, U, U, C}},
    {13, {S, S, U, U, U, U, U, U, U, U, U, C, C}},
    {13, {S, S, S, S, U, U, U, U, U, U, U, C, C}},
    {3, {S, U, C}},
    {13, {S, S, S, S, S, S, S, S, S, U, U, U, C}},
    {10, {S, S, S, S, S, S, S, U, U, C}},
    {13, {S, S, S, S, S, S, S, S, S, U, U, U, C}},
};
static const uint32_t *const SPV[P_N][2] = {
    {fsr3_prepare_inputs_ldr_spv, fsr3_prepare_inputs_hdr_spv},
    {fsr3_luma_pyramid_ldr_spv, fsr3_luma_pyramid_hdr_spv},
    {fsr3_shading_change_pyramid_ldr_spv, fsr3_shading_change_pyramid_hdr_spv},
    {fsr3_shading_change_ldr_spv, fsr3_shading_change_hdr_spv},
    {fsr3_prepare_reactivity_ldr_spv, fsr3_prepare_reactivity_hdr_spv},
    {fsr3_luma_instability_ldr_spv, fsr3_luma_instability_hdr_spv},
    {fsr3_accumulate_ldr_spv, fsr3_accumulate_hdr_spv},
};
static const size_t SPV_BYTES[P_N][2] = {
    {sizeof fsr3_prepare_inputs_ldr_spv, sizeof fsr3_prepare_inputs_hdr_spv},
    {sizeof fsr3_luma_pyramid_ldr_spv, sizeof fsr3_luma_pyramid_hdr_spv},
    {sizeof fsr3_shading_change_pyramid_ldr_spv, sizeof fsr3_shading_change_pyramid_hdr_spv},
    {sizeof fsr3_shading_change_ldr_spv, sizeof fsr3_shading_change_hdr_spv},
    {sizeof fsr3_prepare_reactivity_ldr_spv, sizeof fsr3_prepare_reactivity_hdr_spv},
    {sizeof fsr3_luma_instability_ldr_spv, sizeof fsr3_luma_instability_hdr_spv},
    {sizeof fsr3_accumulate_ldr_spv, sizeof fsr3_accumulate_hdr_spv},
};

// The internal images (AMD's FFX_FSR3UPSCALER_RESOURCE_IDENTIFIER_*): R render size, H half the render size, O output
// size, 1 a single texel. INTER is AMD's INTERMEDIATE_FP16x1: the farthest depth, later the luma instability. LHIST is
// RGBA8, the format the GLSL declares its storage image with (the SDK's HLSL path makes it RGBA16F; a storage view's
// format must match the shader's declaration in Vulkan).
enum { I_DMV, I_DDEPTH, I_RECON, I_INTER, I_FDMIP1, I_LUMA0, I_LUMA1, I_SPD, I_ATOMIC, I_FINFO, I_SHADE, I_DREACT,
       I_NLOCK, I_ACC0, I_ACC1, I_UPS0, I_UPS1, I_LHIST0, I_LHIST1, I_DEF_R, I_DEF_E, I_N };
static const struct { VkFormat f; char size; } IMG[I_N] = {
    {VK_FORMAT_R16G16_SFLOAT, 'R'}, {VK_FORMAT_R32_SFLOAT, 'R'}, {VK_FORMAT_R32_UINT, 'R'},
    {VK_FORMAT_R16_SFLOAT, 'R'}, {VK_FORMAT_R16_SFLOAT, 'H'}, {VK_FORMAT_R16_SFLOAT, 'R'},
    {VK_FORMAT_R16_SFLOAT, 'R'}, {VK_FORMAT_R16G16_SFLOAT, 'H'}, {VK_FORMAT_R32_UINT, '1'},
    {VK_FORMAT_R32G32B32A32_SFLOAT, '1'}, {VK_FORMAT_R8_UNORM, 'H'}, {VK_FORMAT_R8G8B8A8_UNORM, 'R'},
    {VK_FORMAT_R8_UNORM, 'O'}, {VK_FORMAT_R8_UNORM, 'R'}, {VK_FORMAT_R8_UNORM, 'R'},
    {VK_FORMAT_R16G16B16A16_SFLOAT, 'O'}, {VK_FORMAT_R16G16B16A16_SFLOAT, 'O'}, {VK_FORMAT_R8G8B8A8_UNORM, 'R'},
    {VK_FORMAT_R8G8B8A8_UNORM, 'R'}, {VK_FORMAT_R8_UNORM, '1'}, {VK_FORMAT_R32G32_SFLOAT, '1'},
};
#define SPD_VIEWS 6   // the SPD mip levels the pyramid passes write (FSR3UPSCALER_BIND_UAV_SPD_MIPS_LEVEL_0..5)
#define SLOTS 16u     // frames of constant blocks the GPU may still read

// AMD's cbFSR3UPSCALER_t (ffx_fsr3upscaler_callbacks_glsl.h), std140: every member falls on its natural offset here.
typedef struct {
    int32_t render[2], prev_render[2], upscale[2], prev_upscale[2], max_render[2], max_upscale[2];
    float device_to_view[4];
    float jitter[2], prev_jitter[2], mv_scale[2], downscale[2], mv_jitter_cancel[2];
    float tan_half_fov, jitter_len, dt, delta_pre_exposure, view_to_meters, frame_index;
    float velocity, reactiveness, shading_change, accum_added, min_disocclusion;
} Consts;
typedef struct { uint32_t mips, groups, offset[2], render[2]; } SpdConsts;   // cbSPD_t

static struct {
    int tried, ok;
    VkSampler samp[2];
    VkDescriptorSetLayout dsl[P_N];
    VkPipelineLayout layout[P_N];
    VkPipeline pipe[P_N][2];   // ldr, hdr; made at first use
    VkBuffer cb;
    VmaAllocation cb_mem;
    uint8_t *cb_map;
    uint32_t cb_stride, cb_spd, slot;
    // the context: images for one size
    uint32_t w, h, ow, oh, spd_levels;
    VkImage img[I_N];
    VmaAllocation mem[I_N];
    VkImageView view[I_N];
    VkImageView mip[SPD_VIEWS];   // I_SPD's levels as storage views
    uint32_t frame;               // AMD's resourceFrameIndex: which of each image pair is read and which written
    int fresh;                    // the images were just made: the first frame clears the histories
    Consts k;                     // the last frame's constants (the previous-frame fields come from it)
} F;

static void destroy_images(void) {
    const VkCtx *c = vk_ctx();
    for (int i = 0; i < SPD_VIEWS; i++)
        if (F.mip[i]) vkDestroyImageView(c->device, F.mip[i], NULL), F.mip[i] = VK_NULL_HANDLE;
    for (int i = 0; i < I_N; i++) {
        if (F.view[i]) vkDestroyImageView(c->device, F.view[i], NULL);
        if (F.img[i]) vmaDestroyImage(c->vma, F.img[i], F.mem[i]);
        F.view[i] = VK_NULL_HANDLE, F.img[i] = VK_NULL_HANDLE;
    }
    F.w = F.h = F.ow = F.oh = 0;
}

static VkImageView make_view(VkImage img, VkFormat f, uint32_t level, uint32_t levels) {
    VkImageViewCreateInfo vi = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = img,
                                .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = f,
                                .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, level, levels, 0, 1}};
    VkImageView v = VK_NULL_HANDLE;
    return vkCreateImageView(vk_ctx()->device, &vi, NULL, &v) == VK_SUCCESS ? v : VK_NULL_HANDLE;
}

static int make_images(VkCommandBuffer cb, uint32_t w, uint32_t h, uint32_t ow, uint32_t oh) {
    const VkCtx *c = vk_ctx();
    vk_queue_lock();   // the old images may still be in flight; vkDeviceWaitIdle wants every queue held
    vkDeviceWaitIdle(c->device);
    vk_queue_unlock();
    destroy_images();
    uint32_t hw = w / 2 ? w / 2 : 1, hh = h / 2 ? h / 2 : 1, big = hw > hh ? hw : hh;
    F.spd_levels = 1;
    while (big >>= 1) F.spd_levels++;
    VkImageMemoryBarrier2 ib[I_N];
    for (int i = 0; i < I_N; i++) {
        uint32_t iw = IMG[i].size == 'R' ? w : IMG[i].size == 'H' ? hw : IMG[i].size == 'O' ? ow : 1;
        uint32_t ih = IMG[i].size == 'R' ? h : IMG[i].size == 'H' ? hh : IMG[i].size == 'O' ? oh : 1;
        uint32_t levels = i == I_SPD ? F.spd_levels : 1;
        VkImageCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D,
                                .format = IMG[i].f, .extent = {iw, ih, 1}, .mipLevels = levels, .arrayLayers = 1,
                                .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL,
                                .usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                                         VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT};
        VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE};
        if (vmaCreateImage(c->vma, &ci, &ai, &F.img[i], &F.mem[i], NULL) != VK_SUCCESS) {
            F.img[i] = VK_NULL_HANDLE;
            destroy_images();
            return 0;
        }
        if (!(F.view[i] = make_view(F.img[i], IMG[i].f, 0, levels))) {
            destroy_images();
            return 0;
        }
        ib[i] = (VkImageMemoryBarrier2){.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2,
                                        .srcStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
                                        .dstStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
                                        .dstAccessMask = VK_ACCESS_2_MEMORY_READ_BIT | VK_ACCESS_2_MEMORY_WRITE_BIT,
                                        .oldLayout = VK_IMAGE_LAYOUT_UNDEFINED, .newLayout = VK_IMAGE_LAYOUT_GENERAL,
                                        .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
                                        .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .image = F.img[i],
                                        .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, levels, 0, 1}};
    }
    for (uint32_t m = 0; m < SPD_VIEWS; m++)
        if (!(F.mip[m] = make_view(F.img[I_SPD], IMG[I_SPD].f, m < F.spd_levels ? m : F.spd_levels - 1, 1))) {
            destroy_images();
            return 0;
        }
    VkDependencyInfo di = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .imageMemoryBarrierCount = I_N,
                           .pImageMemoryBarriers = ib};
    vkCmdPipelineBarrier2(cb, &di);
    F.w = w, F.h = h, F.ow = ow, F.oh = oh, F.frame = 0, F.fresh = 1;
    memset(&F.k, 0, sizeof F.k);
    rt_log("fsr3: context %ux%u -> %ux%u (%u SPD levels)", w, h, ow, oh, F.spd_levels);
    return 1;
}

static VkPipeline pipeline(int p, int hdr);

static int init(void) {
    F.tried = 1;
    const VkCtx *c = vk_ctx();
    VkSamplerCreateInfo si = {.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, .mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST,
                              .addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                              .addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                              .addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE, .maxLod = VK_LOD_CLAMP_NONE};
    if (vkCreateSampler(c->device, &si, NULL, &F.samp[0]) != VK_SUCCESS) return 0;   // s_PointClamp
    si.magFilter = si.minFilter = VK_FILTER_LINEAR, si.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
    if (vkCreateSampler(c->device, &si, NULL, &F.samp[1]) != VK_SUCCESS) return 0;   // s_LinearClamp
    for (int p = 0; p < P_N; p++) {
        VkDescriptorSetLayoutBinding b[15];
        uint32_t n = 0;
        static const VkDescriptorType T[3] = {VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                              VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER};
        for (uint32_t i = 0; i < LAYOUT[p].n; i++)
            b[n++] = (VkDescriptorSetLayoutBinding){i, T[LAYOUT[p].type[i]], 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL};
        b[n++] = (VkDescriptorSetLayoutBinding){1000, VK_DESCRIPTOR_TYPE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL};
        b[n++] = (VkDescriptorSetLayoutBinding){1001, VK_DESCRIPTOR_TYPE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL};
        VkDescriptorSetLayoutCreateInfo dci = {.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                               .flags = VK_DESCRIPTOR_SET_LAYOUT_CREATE_PUSH_DESCRIPTOR_BIT_KHR,
                                               .bindingCount = n, .pBindings = b};
        VkPipelineLayoutCreateInfo li = {.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                         .pSetLayouts = &F.dsl[p]};
        if (vkCreateDescriptorSetLayout(c->device, &dci, NULL, &F.dsl[p]) != VK_SUCCESS ||
            vkCreatePipelineLayout(c->device, &li, NULL, &F.layout[p]) != VK_SUCCESS)
            return 0;
    }
    VkDeviceSize al = c->props.limits.minUniformBufferOffsetAlignment;
    F.cb_spd = (uint32_t)((sizeof(Consts) + al - 1) / al * al);
    F.cb_stride = (uint32_t)((F.cb_spd + sizeof(SpdConsts) + al - 1) / al * al);
    VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = (VkDeviceSize)SLOTS * F.cb_stride,
                             .usage = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT};
    VmaAllocationCreateInfo ai = {.usage = VMA_MEMORY_USAGE_AUTO_PREFER_HOST,
                                  .flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT |
                                           VMA_ALLOCATION_CREATE_MAPPED_BIT};
    VmaAllocationInfo minfo;
    if (vmaCreateBuffer(c->vma, &bi, &ai, &F.cb, &F.cb_mem, &minfo) != VK_SUCCESS) return 0;
    F.cb_map = minfo.pMappedData;
    // every pass in both variants now, not at the first FSR 3 frame: a mode or HDR change would otherwise stall a frame
    uint64_t t0 = plat_now_ns();
    for (int p = 0; p < P_N; p++)
        for (int hdr = 0; hdr < 2; hdr++)
            if (!pipeline(p, hdr)) {
                rt_log("fsr3: pass %d (%s) has no pipeline", p, hdr ? "hdr" : "ldr");
                return 0;
            }
    rt_log("fsr3: AMD FSR 3.1.4 ready, %d pipelines in %.1f ms", P_N * 2, (plat_now_ns() - t0) / 1e6);
    return F.ok = 1;
}

// Set up at the first call after the pipeline module made its push-descriptor entry point (before the first draw, no
// FSR 3 frame can come anyway).
int vk_fsr3_ready(void) {
    if (!F.tried && vk_ctx() && vk_pipeline_push_fn() && !init())
        rt_log("fsr3: the FSR 3 host could not be set up: FSR 3 off");
    return F.ok;
}

static VkPipeline pipeline(int p, int hdr) {
    if (F.pipe[p][hdr]) return F.pipe[p][hdr];
    const VkCtx *c = vk_ctx();
    VkShaderModuleCreateInfo mi = {.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = SPV_BYTES[p][hdr],
                                   .pCode = SPV[p][hdr]};
    VkShaderModule m = VK_NULL_HANDLE;
    if (vkCreateShaderModule(c->device, &mi, NULL, &m) != VK_SUCCESS) return VK_NULL_HANDLE;
    VkComputePipelineCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
                                      .stage = {.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,
                                                .stage = VK_SHADER_STAGE_COMPUTE_BIT, .module = m, .pName = "main"},
                                      .layout = F.layout[p]};
    if (vkCreateComputePipelines(c->device, VK_NULL_HANDLE, 1, &ci, NULL, &F.pipe[p][hdr]) != VK_SUCCESS)
        F.pipe[p][hdr] = VK_NULL_HANDLE;
    vkDestroyShaderModule(c->device, m, NULL);
    return F.pipe[p][hdr];
}

static void barrier(VkCommandBuffer cb) {
    VkMemoryBarrier2 mb = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER_2,
                           .srcStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
                           .srcAccessMask = VK_ACCESS_2_MEMORY_WRITE_BIT,
                           .dstStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT,
                           .dstAccessMask = VK_ACCESS_2_MEMORY_READ_BIT | VK_ACCESS_2_MEMORY_WRITE_BIT};
    VkDependencyInfo di = {.sType = VK_STRUCTURE_TYPE_DEPENDENCY_INFO, .memoryBarrierCount = 1, .pMemoryBarriers = &mb};
    vkCmdPipelineBarrier2(cb, &di);
}

static void clear(VkCommandBuffer cb, int i, VkClearColorValue v) {
    VkImageSubresourceRange r = {VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_REMAINING_MIP_LEVELS, 0, 1};
    vkCmdClearColorImage(cb, F.img[i], VK_IMAGE_LAYOUT_GENERAL, &v, 1, &r);
}

// One pass: its bindings in order (an image view per S or U binding; C bindings take the frame's constant blocks, the
// main one first, then SPD's), then the dispatch and a barrier.
static int pass(VkCommandBuffer cb, int p, int hdr, const VkImageView *views, uint32_t slot, uint32_t gx, uint32_t gy) {
    VkPipeline pl = pipeline(p, hdr);
    if (!pl) return 0;
    VkDescriptorImageInfo ii[15];
    VkDescriptorBufferInfo bi[2];
    VkWriteDescriptorSet w[15];
    uint32_t nv = 0, nc = 0;
    for (uint32_t i = 0; i < LAYOUT[p].n; i++) {
        int t = LAYOUT[p].type[i];
        w[i] = (VkWriteDescriptorSet){.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstBinding = i,
                                      .descriptorCount = 1};
        if (t == C) {
            VkDeviceSize off = (VkDeviceSize)slot * F.cb_stride + (nc ? F.cb_spd : 0);
            bi[nc] = (VkDescriptorBufferInfo){F.cb, off, nc ? sizeof(SpdConsts) : sizeof(Consts)};
            w[i].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, w[i].pBufferInfo = &bi[nc++];
        } else {
            ii[i] = (VkDescriptorImageInfo){VK_NULL_HANDLE, views[nv++], VK_IMAGE_LAYOUT_GENERAL};
            w[i].descriptorType = t == S ? VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE : VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
            w[i].pImageInfo = &ii[i];
        }
    }
    uint32_t n = LAYOUT[p].n;
    for (uint32_t s = 0; s < 2; s++, n++) {   // the point and linear clamp samplers, pushed with the rest
        ii[n] = (VkDescriptorImageInfo){F.samp[s], VK_NULL_HANDLE, VK_IMAGE_LAYOUT_UNDEFINED};
        w[n] = (VkWriteDescriptorSet){.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstBinding = 1000 + s,
                                      .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_SAMPLER,
                                      .pImageInfo = &ii[n]};
    }
    vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pl);
    rec_push(vk_pipeline_push_fn(), cb, VK_PIPELINE_BIND_POINT_COMPUTE, F.layout[p], 0, n, w);
    vkCmdDispatch(cb, gx, gy, 1);
    barrier(cb);
    return 1;
}

// AMD's setupDeviceDepthToViewSpaceDepthParams for depth near 0 to far 1, not infinite.
static void device_to_view(const Fsr3Eval *e, float out[4]) {
    float n = fminf(e->near_z, e->far_z), f = fmaxf(e->near_z, e->far_z);
    float q = f / (n - f);
    float aspect = (float)e->w / (float)e->h, cot = 1.0f / tanf(0.5f * e->fov_y);
    out[0] = -q, out[1] = q * n, out[2] = aspect / cot, out[3] = 1.0f / cot;
}

int vk_fsr3_evaluate(VkCommandBuffer cb, const Fsr3Eval *e) {
    if (!vk_fsr3_ready()) return 0;
    if ((F.w != e->w || F.h != e->h || F.ow != e->ow || F.oh != e->oh) && !make_images(cb, e->w, e->h, e->ow, e->oh)) {
        rt_log("fsr3: the work images for %ux%u -> %ux%u could not be made: FSR 3 off", e->w, e->h, e->ow, e->oh);
        return 0;
    }
    int hdr = e->hdr != 0, reset = e->reset || F.fresh;
    int odd = F.frame & 1;
    // AMD's pairs: odd frames read the second image of each and write the first, even frames the other way round
    int acc_r = odd ? I_ACC1 : I_ACC0, acc_w = odd ? I_ACC0 : I_ACC1;
    int ups_r = odd ? I_UPS1 : I_UPS0, ups_w = odd ? I_UPS0 : I_UPS1;
    int lh_r = odd ? I_LHIST1 : I_LHIST0, lh_w = odd ? I_LHIST0 : I_LHIST1;
    int luma = odd ? I_LUMA1 : I_LUMA0, luma_prev = odd ? I_LUMA0 : I_LUMA1;
    int expo = hdr ? I_FINFO : I_DEF_E;   // auto exposure on linear light (as DLSS's AutoExposure), else 1

    // the constants (fsr3upscalerDispatch)
    Consts k = F.k;
    memcpy(k.prev_render, F.k.render, sizeof k.prev_render);
    memcpy(k.prev_upscale, F.k.upscale, sizeof k.prev_upscale);
    memcpy(k.prev_jitter, F.k.jitter, sizeof k.prev_jitter);
    k.render[0] = (int32_t)e->w, k.render[1] = (int32_t)e->h;
    k.upscale[0] = (int32_t)e->ow, k.upscale[1] = (int32_t)e->oh;
    k.max_render[0] = (int32_t)e->w, k.max_render[1] = (int32_t)e->h;
    k.max_upscale[0] = (int32_t)e->ow, k.max_upscale[1] = (int32_t)e->oh;
    device_to_view(e, k.device_to_view);
    k.jitter[0] = e->jx, k.jitter[1] = e->jy;
    k.mv_scale[0] = 1.0f / (float)e->w, k.mv_scale[1] = 1.0f / (float)e->h;   // pixels to UV
    k.downscale[0] = (float)e->w / (float)e->ow, k.downscale[1] = (float)e->h / (float)e->oh;
    k.mv_jitter_cancel[0] = k.mv_jitter_cancel[1] = 0;
    k.tan_half_fov = tanf(0.5f * e->fov_y) * (float)e->w / (float)e->h;   // horizontal, from the vertical one
    k.jitter_len = (float)e->phases;
    k.dt = fmaxf(0.0f, fminf(1.0f, e->dt_ms / 1000.0f));
    k.delta_pre_exposure = 1.0f, k.view_to_meters = 1.0f;
    k.frame_index = reset ? 0.0f : F.k.frame_index + 1.0f;
    k.velocity = 1.0f, k.reactiveness = 1.0f, k.shading_change = 1.0f;
    k.accum_added = 1.0f / 3.0f, k.min_disocclusion = -1.0f / 3.0f;
    F.k = k;
    uint32_t sx = (e->w + 63) / 64, sy = (e->h + 63) / 64, big = e->w > e->h ? e->w : e->h, mips = 0;
    while (big >>= 1) mips++;
    SpdConsts sp = {mips > 12 ? 12 : mips, sx * sy, {0, 0}, {e->w, e->h}};
    uint32_t slot = F.slot++ % SLOTS;
    memcpy(F.cb_map + (size_t)slot * F.cb_stride, &k, sizeof k);
    memcpy(F.cb_map + (size_t)slot * F.cb_stride + F.cb_spd, &sp, sizeof sp);
    vmaFlushAllocation(vk_ctx()->vma, F.cb_mem, (VkDeviceSize)slot * F.cb_stride, F.cb_stride);

    // the clears AMD's dispatch schedules ahead of the passes
    VkClearColorValue zero = {{0, 0, 0, 0}};
    if (F.fresh)   // new images: every one starts from zero (AMD clears the accumulations and lumas; the rest is
        for (int i = 0; i < I_N; i++) clear(cb, i, zero);   // written before it is read, or is a 1x1 default)
    if (reset) {
        clear(cb, acc_r, zero);
        VkClearColorValue fi = {{-1.0f, 1.0f, 0, 0}};   // no scene luminance yet
        clear(cb, I_FINFO, fi);
    }
    VkClearColorValue far = {.uint32 = {0x3f800000u, 0, 0, 0}};   // 1.0f: the far plane, which atomicMin lowers
    clear(cb, I_RECON, far);
    clear(cb, I_ATOMIC, zero);
    clear(cb, I_SPD, zero);
    barrier(cb);
    F.fresh = 0;

    uint32_t gx = (e->w + 7) / 8, gy = (e->h + 7) / 8, ox = (e->ow + 7) / 8, oy = (e->oh + 7) / 8;
    uint32_t hx = (e->w / 2 + 7) / 8, hy = (e->h / 2 + 7) / 8;
    const VkImageView v_inputs[] = {e->mv, e->depth, e->color, F.view[I_DMV], F.view[I_DDEPTH], F.view[I_RECON],
                                    F.view[I_INTER], F.view[luma]};
    const VkImageView v_luma_pyr[] = {F.view[luma], F.view[I_INTER], F.view[I_ATOMIC], F.view[I_FINFO], F.mip[0],
                                      F.mip[1], F.mip[2], F.mip[3], F.mip[4], F.mip[5], F.view[I_FDMIP1]};
    const VkImageView v_shade_pyr[] = {F.view[luma], F.view[luma_prev], F.view[I_DMV], F.view[expo], F.view[I_ATOMIC],
                                       F.mip[0], F.mip[1], F.mip[2], F.mip[3], F.mip[4], F.mip[5]};
    const VkImageView v_shade[] = {F.view[I_SPD], F.view[I_SHADE]};
    const VkImageView v_react[] = {F.view[I_RECON], F.view[I_DMV], F.view[I_DDEPTH], F.view[I_DEF_R], F.view[I_DEF_R],
                                   F.view[acc_r], F.view[I_SHADE], F.view[luma], F.view[expo], F.view[I_DREACT],
                                   F.view[I_NLOCK], F.view[acc_w]};
    const VkImageView v_instab[] = {F.view[expo], F.view[I_DREACT], F.view[I_DMV], F.view[I_FINFO], F.view[lh_r],
                                    F.view[I_FDMIP1], F.view[luma], F.view[lh_w], F.view[I_INTER]};
    const VkImageView v_accum[] = {F.view[expo], F.view[I_DREACT], F.view[I_DMV], F.view[ups_r], F.view[I_DEF_R],
                                   F.view[I_FDMIP1], F.view[luma], F.view[I_INTER], e->color, F.view[ups_w], e->out,
                                   F.view[I_NLOCK]};
    int ok = pass(cb, P_PREPARE_INPUTS, hdr, v_inputs, slot, gx, gy) &&
             pass(cb, P_LUMA_PYRAMID, hdr, v_luma_pyr, slot, sx, sy) &&
             pass(cb, P_SHADING_PYRAMID, hdr, v_shade_pyr, slot, sx, sy) &&
             pass(cb, P_SHADING_CHANGE, hdr, v_shade, slot, hx, hy) &&
             pass(cb, P_PREPARE_REACTIVITY, hdr, v_react, slot, gx, gy) &&
             pass(cb, P_LUMA_INSTABILITY, hdr, v_instab, slot, gx, gy) &&
             pass(cb, P_ACCUMULATE, hdr, v_accum, slot, ox, oy);
    if (!ok) {
        rt_log("fsr3: a pass's pipeline could not be made: FSR 3 off");
        return 0;
    }
    F.frame = (F.frame + 1) % 16u;   // AMD's FSR3UPSCALER_MAX_QUEUED_FRAMES; only its parity is used
    return 1;
}
