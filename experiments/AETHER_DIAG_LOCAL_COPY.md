# Controlled aether-client-cpp copy for Wi-Fi E2E diagnostics

Pin: `157aadbec8e7b852d0f89274307ff7cb8103e5f7`

Apply `experiments/aether-157aadbe-wifi-e2e-diag.patch` to a detached clone (do not patch shared CPM cache):

```powershell
git clone <aether-client-cpp-url> deps/aether-client-cpp-157aadbe-diag
cd deps/aether-client-cpp-157aadbe-diag
git checkout 157aadbec8e7b852d0f89274307ff7cb8103e5f7
git apply ../../experiments/aether-157aadbe-wifi-e2e-diag.patch
```

Runner sets `-DCPM_aether-client-cpp_SOURCE=.../deps/aether-client-cpp-157aadbe-diag`.

New file in patch: `aether/wifi/wifi_drv_diag_log.h` (included by patched `esp_wifi_driver.cpp`).
