# Third-party notices

The runtime depends on upstream projects rather than bundling their repositories.

- mini-SWE-agent: https://github.com/SWE-agent/mini-swe-agent, pinned in configs/upstream_versions.json.
- Pier: https://github.com/datacurve-ai/pier, Apache-2.0; includes Harbor-derived code under its upstream notices.
- DeepSWE: https://github.com/datacurve-ai/deep-swe; obtain the dataset separately under its upstream terms.

The continuation control flow in src/glm_trace/checkpoint.py follows the pinned mini-SWE-agent DefaultAgent loop, with restoration of saved messages, counters and active time in place of fresh initialization. The upstream mini-SWE-agent license notice is retained below. This notice does not assign a new license to the rest of this repository.

## mini-SWE-agent

MIT License

Copyright (c) 2025 Kilian A. Lieret and Carlos E. Jimenez

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.