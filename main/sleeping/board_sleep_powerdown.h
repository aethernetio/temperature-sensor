/*
 * Copyright 2026 Aethernet Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#ifndef SLEEPING_BOARD_SLEEP_POWERDOWN_H_
#define SLEEPING_BOARD_SLEEP_POWERDOWN_H_

#if defined(ESP_PLATFORM)
#ifdef __cplusplus
extern "C" {
#endif

/* Quiet Thermometer 2 rails (GPIO/ULP) before deep sleep. No flash-window delays. */
void BoardPowerDownForDeepSleep(void);

/* Force available PD domains OFF (same set used by B1 sleep-only ~7 µA). */
void BoardApplyMinPowerDomains(void);

/* Keep RTC slow/fast memory powered for RTC_DATA_ATTR caches. */
void BoardRetainRtcMemoryOn(void);

/* Prefer AUTO retention where the SoC keeps RTC_DATA_ATTR correctly. */
void BoardRetainRtcMemoryAuto(void);

/*
 * Full pre-deep-sleep prep: GPIO/ULP quiet + min PD domains + RTC mem policy.
 * retain_rtc_mem_on: true → RTC_*_MEM ON (UDP/prepared caches);
 *                    false → leave domains as ApplyMin left them (OFF).
 */
void BoardPrepareDeepSleep(int retain_rtc_mem_on);

#ifdef __cplusplus
}
#endif
#endif

#endif  // SLEEPING_BOARD_SLEEP_POWERDOWN_H_
