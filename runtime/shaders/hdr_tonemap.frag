#version 450
// Astral Chain's tonemap pass for HDR output (runtime/vk_hdr.c vk_hdr_shader_swap puts it in place of the translated
// shader when AC_HDR=1). The game's shader, as measured (AC_DUMP: one full-screen draw into the R11G11B10F scene target
// after the bloom, before the SMAA passes and the HUD):
//   exposure (auto exposure from fp_s0 or the 1x1 adaptation texture, then exp2 of an EV), a filmic curve: Hable's when
//   c4[6].w is 0 (B = 0.5 and C = 0.1 built in, the rest in c1), Uchimura's (GT) when it is above 0 (the parameters in
//   c4[4] and c4[5], the shoulder's ceiling P in c4[4].x), a vignette, a clamp to 0..1, the sRGB encode, a 16x16x16
//   colour LUT stored as 256x16 (blue picks the slice), a gamma (c4[1].x) and a fade towards (0, 0.078, 0.1568)
//   by c4[0].w. Its output is sRGB-encoded; the SMAA passes and the HUD then work in 8 bits.
// Here the SDR result is computed exactly as the game does. With the HDR swapchain active (support_buffer.hdr.y > 1:
// the peak over the game's white) the curve's ceiling becomes that ratio (Uchimura: P scaled, the toe and the linear
// part unchanged; Hable: a roll-off from max(1, peak / 2) to the peak instead of the clamp), and the graded SDR colour is
// scaled by the ratio of the extended curve's luminance to the clamped one's, so the LUT's grade and the hue stay. The
// result is written sRGB-encoded with the curve continued above 1; the present (shaders/hdr_encode.comp) decodes it.
// Hue correction (hdr.z, AC_HDR_HUE): that ratio keeps the hue of the game's clamped SDR colour, where bright saturated
// colours turn towards white (fire clips red, then green, and reads yellow-white, the colour of the light it casts).
// Below 1 the result is that; from the picture's white upwards it moves, by 1 - hdr.z, to the extended curve's own
// channel ratios at the same luminance, which keep such colours saturated (TotK Recomp's hdr_hue, after RenoDX's TotK
// mod; there the control runs the other way, from the extended curve towards the SDR result).
// Approach after RenoDX (github.com/clshortfuse/renodx, MIT): grade in SDR, then restore the highlights by a ratio.
// Bindings are those of the game's translated shader: vk_hdr.c rewrites them from the original's names.
layout(location = 0) in vec4 uv;
layout(location = 0) out vec4 color;

layout(set = 0, binding = 0, std140) uniform SupportBuffer {
    vec4 pad[90];
    vec4 hdr;   // byte 1440 (vk_exec_draw.c support_fill): y = peak / game white (1 while SDR), z = hue correction
} support_buffer;
layout(set = 0, binding = 1) uniform sampler2D fp_t_tcb_8;    // the lit scene
layout(set = 0, binding = 2) uniform sampler2D fp_t_tcb_C;    // the bloom, added after the curve
layout(set = 0, binding = 3) uniform sampler2D fp_t_tcb_10;   // the adaptation (green at the centre)
layout(set = 0, binding = 4) uniform sampler2D fp_t_tcb_A;    // the colour LUT, 256x16
layout(set = 0, binding = 5, std430) readonly buffer S0 { uint v[]; } fp_s0;   // the auto exposure's average
layout(set = 0, binding = 6, std140) uniform C4 { vec4 v[4096]; } fp_c4;
layout(set = 0, binding = 7, std140) uniform C0 { vec4 v[4096]; } fp_c0;
layout(set = 0, binding = 8, std140) uniform C1 { vec4 v[4096]; } fp_c1;

vec4 tex(sampler2D s, vec2 p) { return texture(s, mix(p, vec2(0.0), isnan(p))); }   // the translator's NaN guard

float lum(vec3 c) { return dot(c, vec3(0.2126, 0.7152, 0.0722)); }
float maxc(vec3 c) { return max(c.r, max(c.g, c.b)); }

// sRGB encode as the game writes it (c1[1].z: the linear part's end, c1[1].w: 1.055), and continued above 1
float encode1(float c, float lim, float k) {
    float e = clamp(c * 12.92, 0.0, 1.0);
    if (!(c <= lim)) e = clamp(pow(abs(c), 0.416666657) * k - 0.055, 0.0, 1.0);
    return e;
}
vec3 encode_ext(vec3 c) {
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, greaterThan(c, vec3(0.0031308)));
}
vec3 decode(vec3 c) {
    return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), greaterThan(c, vec3(0.04045)));
}

// Hable's curve with the game's constants, white-scaled; the bloom is added after it
vec3 hable(vec3 x, vec3 bloom) {
    vec4 a = fp_c1.v[0], b = fp_c1.v[1];
    vec3 num = x * (x * a.y + 0.05) + a.w, den = x * (x * a.y + 0.5) + a.z;
    return (num / den + b.x) * b.y + bloom;
}

// Uchimura's curve: toe T, linear part L, shoulder S rising to the ceiling ceil_ (the game's c4[4].x in SDR)
vec3 uchimura(vec3 x, float ceil_) {
    vec4 p4 = fp_c4.v[4], p5 = fp_c4.v[5];
    float inv_m = 1.0 / p4.z, room = p5.y + (ceil_ - p4.x);   // ceiling minus the shoulder's start value
    vec3 t = clamp(x * inv_m, 0.0, 1.0);
    vec3 w0 = (t * -2.0 + 3.0) * t * t;
    vec3 toe = pow(abs(x * inv_m), vec3(p4.w)) * p5.z + p5.w;
    vec3 w2 = vec3(greaterThan(x, vec3(p5.x)));
    vec3 sh = exp2((p5.x - x) * p4.y * (1.0 / room) * 1.44269502) * -room + ceil_;
    vec3 lin = (x - p4.z) * p4.y + p5.z;
    return w2 * sh + lin * (w0 - w2) + (w0 * -toe + toe);
}

void main() {
    vec2 at = uv.xy;
    vec3 scene = tex(fp_t_tcb_8, at).rgb, bloom = tex(fp_t_tcb_C, at).rgb;
    float ratio = support_buffer.hdr.y;
    bool hdr = ratio > 1.0;
    vec4 c4_0 = fp_c4.v[0], c4_1 = fp_c4.v[1], c4_2 = fp_c4.v[2], c4_6 = fp_c4.v[6];

    vec3 v = scene, t = scene, th = scene;   // th: the extended picture
    if (c4_6.w == 0.0) {
        if (c4_1.w >= 0.0) {
            int a = floatBitsToInt(fp_c0.v[86].x);
            float avg = uintBitsToFloat(fp_s0.v[uint((a - (a & -16)) >> 2)]);
            vec3 s = scene * (1.0 / avg * c4_1.w);
            v = s * s * fp_c1.v[0].x + s;
        }
        t = hable(v * exp2(c4_2.x), bloom);
        th = t;
        if (hdr) {   // a roll-off to the peak where the game clamps
            float top = maxc(th), knee = max(1.0, 0.5 * ratio);
            if (top > knee) th *= (knee + (ratio - knee) * (1.0 - exp(-(top - knee) / (ratio - knee)))) / top;
        }
    }
    if (0.0 < c4_6.z) {
        float adapt = tex(fp_t_tcb_10, vec2(0.5)).g;
        t = adapt * ((bloom * c4_2.z + t) * c4_0.y);
        th = adapt * ((bloom * c4_2.z + th) * c4_0.y);
    }
    if (0.0 < c4_6.w) {
        float ceil_ = fp_c4.v[4].x;
        vec3 x = t;
        t = uchimura(x, ceil_);
        th = hdr ? uchimura(x, ceil_ * ratio) : t;
    }
    vec4 c4_3 = fp_c4.v[3];
    if (0.0 < c4_3.x) {
        float inv = 1.0 / c4_3.x;
        vec2 d = 0.5 - at;
        float vig = pow(abs(clamp(inv * -sqrt(d.y * d.y + d.x * d.x) + inv, 0.0, 1.0)), c4_3.y);
        t *= vig;
        th *= vig;
    }

    // the game's SDR grade
    vec3 c = clamp(t, 0.0, 1.0);
    float lim = fp_c1.v[1].z, k = fp_c1.v[1].w;
    vec3 e = vec3(encode1(c.r, lim, k), encode1(c.g, lim, k), encode1(c.b, lim, k));
    float slice = floor(e.b * 14.9998999);   // blue picks the slice, red runs along it, green down
    float u = slice * 0.0625 + e.r * 0.05859375, w = e.g * 0.9375 + 0.03125;
    vec3 hi = tex(fp_t_tcb_A, vec2(u + 0.064453125, w)).rgb, lo = tex(fp_t_tcb_A, vec2(u + 0.001953125, w)).rgb;
    vec3 g = (hi - lo) * (e.b * 15.0 - slice) + lo;
    g = clamp(pow(abs(g), vec3(c4_1.x)), 0.0, 1.0);
    vec3 fade = vec3(0.0, 0.0780000016, 0.156800002);
    vec3 sdr = (g - fade) * c4_0.w + fade;

    if (hdr) {   // the highlights the clamp took, as a luminance ratio on the graded colour
        float ys = lum(c), yh = lum(max(th, vec3(0.0)));
        float gain = ys > 1e-5 ? yh / ys : 1.0;
        if (gain > 1.0001) {
            vec3 lin = decode(max(sdr, vec3(0.0))) * gain;   // the SDR hue at the HDR luminance
            float yl = lum(lin), yt = lum(max(th, vec3(0.0)));
            float sat = (1.0 - clamp(support_buffer.hdr.z, 0.0, 1.0)) * smoothstep(0.5, 1.5, maxc(th));
            if (sat > 0.0 && yt > 1e-5) lin = mix(lin, max(th, vec3(0.0)) * (yl / yt), sat);
            sdr = encode_ext(lin);
        }
    }
    color = vec4(sdr, 0.0);
}
