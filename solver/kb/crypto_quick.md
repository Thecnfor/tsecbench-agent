# Crypto Quick-Win Cheatsheet

## RSA (compute before searching)
- tiny e (3, 65537) + no padding → small-message cube root; Hastad broadcast for e copies.
- shared prime across two moduli → gcd(N1, N2).
- close primes → Fermat factorization; small n → factordb offline first, then sympy.
- Wiener when d small (e large): continued fractions.
- Given d leak or partial key → reconstruct, don't re-derive.

## Symmetric / block
- Repeating identical ciphertext blocks → ECB (image the pattern with `xxd | awk`).
- XOR: repeating-key via crib "flag{"/"FLAG{" sliding; single-byte via chi-square script.
- CBC bit-flipping on auth tokens: flip the block BEFORE the target field.

## Hashes
- Identify: length 32→md5, 40→sha1, 64→sha256; `$6$`→sha512crypt (hashcat -m 1800).
- CTF hash = often a lookup away: try common wordlists before "cracking" math.

## Misc
- ROT/Atbash/Vigenère: check letter frequency first; Vigenère key length via IC.
- QR: `zbarimg -q img.png`; damaged QR → reconstruct with reedsolomon tolerance.
- RSA/AES key in "random" file → strings/binwalk it before brute force.
