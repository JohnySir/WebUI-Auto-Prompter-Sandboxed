# WebUI Auto Prompter (Sandboxed)

![UI-SS](https://raw.githubusercontent.com/JohnySir/WebUI-Auto-Prompter-Sandboxed-/refs/heads/main/assets/UI.png)

An automated, terminal-driven batch prompter for **Stable Diffusion WebUI (Forge Neo / Classic / A1111)**. It feeds prompt queues directly into the web interface through a dedicated, Playwright-controlled sandbox browser over the Chrome DevTools Protocol (CDP).

---

> [!IMPORTANT]
> ### Prerequisite: Start WebUI Forge Neo First
> **You must launch your WebUI Forge Neo as normal before running this app.**
> 
> WebUI Prompter is a client-side automation driver—it does not launch or manage your Stable Diffusion backend. Make sure your WebUI is running and accessible (default: `http://127.0.0.1:7860`) with the **txt2img** tab open before starting the prompter.

---

## How It Works

WebUI Prompter interacts with the WebUI exactly like a careful human operator, but with machine precision and non-invasive automation:

1. **Dedicated Sandbox Browser Session**:
   - Launches an isolated Chromium instance with its own user profile directory (`.sandbox-profile`) on remote debugging port `9222`.
   - Your primary daily browser, cookies, and open tabs remain completely untouched.
   - The browser process runs detached—closing or quitting the terminal interface never kills the browser or interrupts an ongoing generation.

2. **Conservative State Machine (`READY` / `BUSY` / `UNKNOWN`)**:
   - The automation worker continuously monitors DOM state every `0.4s`.
   - If the Interrupt button (`#txt2img_interrupt`) is visible, state is **`BUSY`**—prompter never clicks.
   - Only when Generate (`#txt2img_generate`) is visible, editable, and enabled does it report **`READY`**.
   - If UI elements cannot be found (e.g., user navigated to another tab), state becomes **`UNKNOWN`** and execution pauses safely with a warning.
   - **No blind retries**: After clicking Generate, it waits up to 20 seconds for `BUSY` state to register; it will never double-click (which could accidentally trigger Interrupt/Skip).

3. **Safe Field Injection (No Clipboard Hijacking)**:
   - Injects each prompt directly into `#txt2img_prompt textarea` and triggers a native DOM `change` event.
   - Immediately reads back the text value to verify that no truncation occurred. If a browser lag occurs, it falls back to focused keyboard entry.
   - Your OS clipboard is never modified or read.

4. **Automatic Theme & Preference Preservation**:
   - Standard CDP attachments force `prefers-color-scheme: light`, which often turns dark-mode WebUIs white. WebUI Prompter reads the active color scheme via raw CDP WebSocket evaluation before attaching, then pins the matching theme via Playwright media emulation.

---

## Installation & Setup

### 1. Install Dependencies

Ensure Python 3.10+ is installed, then install required packages:

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

### 2. Prepare Prompts

Create a `prompts.txt` file (or any `.txt` / `.md` file) in UTF-8 encoding. Place one prompt per line:

```text
A cinematic photo of an astronaut riding a mechanical horse across neon dunes, 8k, photorealistic
Isometric render of a cozy cyberpunk coffee shop during rain, warm lighting, octane render
Oil painting portrait of a wise old owl wearing Victorian scholar robes, Rembrandt lighting
```

---

## How to Run and Use

### Quick Start

1. Start your **WebUI Forge Neo** (verify `http://127.0.0.1:7860` opens in a browser).
2. Double-click `run.bat` or run from terminal:
   ```bash
   python prompter.py
   ```
3. The dedicated browser window will launch (or attach if already open).
4. Use keyboard shortcuts or mouse buttons to drive the queue:
   - <kbd>F8</kbd> or click **Start**: Begins processing prompts from the queue.
   - <kbd>F9</kbd> or click **Stop**: Pauses the queue after the current generation finishes (never interrupts an active render).
   - <kbd>F10</kbd> or click **Quit**: Exits the terminal app (leaves the browser running).

### Preflight Verification (`--check`)

Before running a long queue, you can test whether the WebUI is running and all DOM selectors match:

```bash
python prompter.py --check
```

This tests the CDP connection, inspects the tab title, verifies prompt box presence and editability, checks the Generate button state, and exits with code `0` (ready) or `1` (issue detected).

### CLI Options

```bash
python prompter.py [--file PATH] [--url URL] [--check]

Options:
  --file PATH   Path to prompt file (default: prompts.txt)
  --url URL     URL of the WebUI dashboard (default: http://127.0.0.1:7860)
  --check       Run selector preflight checks and exit
```

---

## How to Add More Automated Features

The modular design makes it easy to extend automation beyond basic prompt entry and clicking Generate. Below are patterns for adding common features inside the worker loop (`run_worker` in `prompter.py`).

### 1. Setting Negative Prompts

Target the negative prompt textarea selector (`#txt2img_neg_prompt textarea`):

```python
# In your worker before clicking Generate:
neg_box = page.locator("#txt2img_neg_prompt textarea")
if neg_box.is_visible():
    neg_box.fill("blurry, low quality, distorted, watermark")
    neg_box.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
```

### 2. Modifying Sliders (Steps, CFG Scale, Width, Height)

Gradio sliders have an input number field nested inside their container ID:

```python
# Set Sampling Steps (e.g., #txt2img_steps)
steps_input = page.locator("#txt2img_steps input[type='number']")
steps_input.fill("30")
steps_input.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")

# Set Width and Height
page.locator("#txt2img_width input[type='number']").fill("1024")
page.locator("#txt2img_height input[type='number']").fill("1024")
```

### 3. Checkpoint & LoRA Cycling

You can cycle checkpoints or add dynamic `<lora:...>` triggers to prompt strings per line:

```python
# Format prompts with embedded tags in prompts.txt:
# [model=flux-dev] A portrait of a woman...
# Parse tags in parse_prompts() and trigger model switch if needed:
checkpoint_dropdown = page.locator("#setting_sd_model_checkpoint")
```

### 4. Post-Generation Webhooks & Notifications

When `state.phase` transitions from `GENERATING` to `READY`, you can dispatch desktop notifications, play audio chimes, or trigger Discord webhooks:

```python
if state.completed > 0:
    # Trigger webhook or play sound
    import winsound
    winsound.MessageBeep()
```

### 5. Multi-Tab Automation (e.g., img2img)

To adapt the prompter for **img2img**, simply update the selector prefixes from `txt2img_*` to `img2img_*`:
- Prompt: `#img2img_prompt textarea`
- Generate Button: `#img2img_generate`
- Interrupt Button: `#img2img_interrupt`

---

## Safety & Stability Rules

- **Non-destructive**: Generation parameter settings, checkpoints, samplers, and output galleries are fully managed by Forge Neo; this tool will never overwrite settings you haven't explicitly scripted.
- **Fail-safe queue**: If connection drops or a prompt fails verification, the queue halts without skipping prompts, allowing you to fix the issue and resume.
