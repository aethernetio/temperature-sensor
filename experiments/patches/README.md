# Aether SDK patches (Stage 1 full vanilla)

## `aether-157aadbe-full-vanilla-stage1.patch`

- **Base commit:** `157aadbec8e7b852d0f89274307ff7cb8103e5f7`
- **Purpose:** Vanilla Wi-Fi path only (`RebuildChannelsFromAdapters`, canonical Wi-Fi driver hooks). Not HOT/prepared Wi-Fi optimizations.
- **Apply:** `experiments/run_thermometer2_aether_full_vanilla.py` calls `ensure_aether_patched()` before ESP/receiver builds.

Manual apply:

```bash
cd deps/aether-client-cpp-157aadbe-diag
git checkout --force 157aadbec8e7b852d0f89274307ff7cb8103e5f7
git apply ../../experiments/patches/aether-157aadbe-full-vanilla-stage1.patch
```

Firmware reproducibility requires this patch on the pinned SDK; the temperature-sensor git SHA alone is not sufficient until the patch is committed and applied in CI/local builds.
