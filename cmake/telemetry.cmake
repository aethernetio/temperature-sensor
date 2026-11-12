# Copyright 2026 Aethernet Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# CPM declarations take precedence over transitive SDK declarations. Keep the
# embedded SDK's prepared-send/sleep lifecycle while using the same telemetry
# branch and codec dependencies as desktop builds.
CPMDeclarePackage(aether-tele
  NAME aether-tele
  GITHUB_REPOSITORY aethernetio/aether-tele
  GIT_TAG main
  GIT_CONFIG core.autocrlf=false
  OPTIONS "AE_TELE_INSTALL ${AE_INSTALL}"
          "AE_TELE_BUILD_TESTS ${AE_BUILD_TESTS}"
  EXCLUDE_FROM_ALL FALSE)
CPMDeclarePackage(ae-numeric
  NAME ae-numeric
  GITHUB_REPOSITORY aethernetio/aethernet-numeric
  GIT_TAG 9d79bd79188420533bab07c437d617aedfaf6b1a
  GIT_CONFIG core.autocrlf=false
  OPTIONS "AE_NUMERIC_INSTALL ${AE_INSTALL}" "AE_BUILD_TESTS OFF"
  EXCLUDE_FROM_ALL FALSE)
CPMDeclarePackage(aether-miscpp
  NAME aether-miscpp
  GITHUB_REPOSITORY aethernetio/aether-miscpp
  GIT_TAG 6839624442621a5b060ae610e91f480278ebaf6e
  GIT_CONFIG core.autocrlf=false
  OPTIONS "AE_INSTALL ${AE_INSTALL}" "AE_BUILD_TESTS OFF"
  EXCLUDE_FROM_ALL FALSE)
