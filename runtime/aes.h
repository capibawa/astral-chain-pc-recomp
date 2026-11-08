// AES-128 (FIPS 197) for reading the game's NCAs in place (romfs_source.c): the block cipher both ways, CTR and the
// NCA header's XTS. CTR uses the x86 AES instructions when the CPU has them, portable C otherwise.
#pragma once
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t enc[176];   // the expanded key, rounds 0..10
    uint8_t dec[176];   // the same in reverse order with InvMixColumns applied (rounds 1..9), for the inverse cipher
} Aes128;

void aes128_init(Aes128 *a, const uint8_t key[16]);
void aes128_encrypt(const Aes128 *a, const uint8_t in[16], uint8_t out[16]);
void aes128_decrypt(const Aes128 *a, const uint8_t in[16], uint8_t out[16]);

// XORs n bytes of data with the CTR keystream whose counter is high (8 bytes, as stored) || block (64 bits,
// big-endian) and counts up per 16-byte block in its low 64 bits. data starts skip (0..15) bytes into that block.
void aes128_ctr(const Aes128 *a, const uint8_t high[8], uint64_t block, unsigned skip, uint8_t *data, size_t n);

// Decrypts one XTS data unit of n bytes (a multiple of 16) in place: k1 the data key, k2 the tweak key. The tweak is
// the unit number as a 16-byte big-endian integer, as Nintendo's NCA headers use it (not the IEEE little-endian one).
void aes128_xts_decrypt(const Aes128 *k1, const Aes128 *k2, uint64_t unit, uint8_t *data, size_t n);
