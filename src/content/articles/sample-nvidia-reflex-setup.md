---
title: "Sample: How to enable NVIDIA Reflex in competitive games"
description: "Sample article: a step-by-step look at turning on NVIDIA Reflex in competitive PC games and what the setting actually changes."
pubDate: 2026-10-07
updatedDate: 2026-10-08
category: pc
tags:
  - nvidia
  - reflex
  - latency
  - settings
  - sample
entities:
  - NVIDIA
  - Reflex
  - GeForce
sources:
  - name: NVIDIA Reflex
    url: https://www.nvidia.com/en-us/geforce/technologies/reflex/
  - name: Wikipedia — Input lag
    url: https://en.wikipedia.org/wiki/Input_lag
---

> **Sample article.** This is placeholder content written to demonstrate the
> GamersXpress article layout. It is not reported news and covers no announcement.

NVIDIA Reflex is a latency-reduction feature built into a long list of
competitive games. The idea is simple: shrink the delay between the moment you
move your mouse and the moment the change appears on screen, so your aim feels
more connected to what is happening in the match. This sample walks through
where the setting lives and what the two modes actually do.

## What the setting changes

Every frame your PC renders sits between your input and your display. That
queue is where input lag comes from. Reflex works by keeping that queue shallow,
limiting how far the render-ahead buffer is allowed to grow when the CPU is
producing frames faster than the GPU can draw them. The result, according to
[NVIDIA's documentation](https://www.nvidia.com/en-us/geforce/technologies/reflex/),
is a more responsive feel in titles that support it. For a broader definition
of the problem being solved, see the overview of
[input lag on Wikipedia](https://en.wikipedia.org/wiki/Input_lag).

## Before you start

- A supported GeForce GPU and current graphics drivers.
- A game that lists Reflex support in its graphics options.
- A display you actually use at a high refresh rate, since the setting cannot
  compensate for a slow panel.

## Turning it on

1. Update your graphics driver through the NVIDIA app or your usual driver
   package.
2. Launch the game and open its video or graphics settings menu.
3. Look for an option labelled **NVIDIA Reflex Low Latency**.
4. Choose **Enabled**, or **Enabled + Boost** if you want the GPU clocks held
   higher during play.

The difference between the two modes is small. Boost keeps the graphics card
running at a higher clock while the GPU is waiting between frames, which
NVIDIA says reduces latency a little further at the cost of some power draw.
Neither mode is a substitute for a stable frame rate.

## Things worth knowing

Reflex only does something in games that implement it. In older or unsupported
titles, the toggle will simply not appear. It also does not fix stutters caused
by background downloads, thermal throttling, or a misconfigured monitor cable,
so it is worth ruling those out first.

Treat any specific latency numbers you see online as system-dependent. Results
vary with hardware, refresh rate, and in-game settings, and this sample does
not reproduce benchmark figures.
