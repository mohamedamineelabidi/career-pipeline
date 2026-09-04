---
version: alpha
name: Reach
description: Privy-derived, implementation-oriented UI for the Reach contact and Morocco job radar. Clean, functional, keyboard-first.
colors:
  primary: "#111117"
  secondary: "#70707d"
  link: "#0000ee"
  surface: "#ffffff"
  surface-raised: "#f7f7f5"
  surface-inverse: "#000000"
  border: "#e4e4e0"
  success: "#0f6b3a"
  warning: "#8a5a00"
  danger: "#a4232b"
  focus: "#0000ee"
typography:
  h1:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 42px
    fontWeight: 600
    lineHeight: 1.1
    letterSpacing: "-0.02em"
  h2:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 26px
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.01em"
  h3:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 20px
    fontWeight: 600
    lineHeight: 1.3
  body-lg:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 18px
    fontWeight: 400
    lineHeight: 1.5
  body-md:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif
    fontSize: 12px
    fontWeight: 500
    lineHeight: 1.4
    letterSpacing: "0.02em"
rounded:
  xs: 8px
  pill: 100px
spacing:
  1: 8px
  2: 12px
  3: 16px
  4: 20px
  5: 28px
  6: 32px
  7: 80px
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.surface}"
    typography: "{typography.body-md}"
    rounded: "{rounded.pill}"
    padding: 12px
  button-primary-hover:
    backgroundColor: "{colors.surface-inverse}"
    textColor: "{colors.surface}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.pill}"
    padding: 12px
  button-secondary-hover:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.primary}"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.xs}"
    padding: 20px
  card-muted:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.primary}"
    rounded: "{rounded.xs}"
    padding: 20px
  badge-neutral:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.secondary}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 8px
  badge-success:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.success}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 8px
  badge-warning:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.warning}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 8px
  badge-danger:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.danger}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: 8px
  nav-item:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    typography: "{typography.body-md}"
    rounded: "{rounded.xs}"
    padding: 12px
  nav-item-active:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.primary}"
  link:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.link}"
    typography: "{typography.body-md}"
---

## Overview

Reach is the second front of Career Pipeline: it lists the right people to
contact at target companies and the Morocco AI / Cloud / Data jobs and
internships found by the radar. The look is borrowed from Privy
(privy.io): white and warm-grey surfaces, near-black ink, pill buttons,
8px corners, lots of air between sections, no decoration that does not
carry information. Audience is one technical user who reads it daily, so
density is welcome but hierarchy must stay obvious at a glance.

Two token corrections versus the crawled Privy values: the crawled
`text.primary=#0000ee` is default link blue and is used here **only** for
links; the crawled `font.size.base=12px` came from the footer and is used
**only** for labels. Body text is 14px.

## Colors

- **primary (#111117):** all body text and headings. 16.9:1 on surface.
- **secondary (#70707d):** metadata, helper text, inactive nav. 4.9:1 on
  surface, 4.6:1 on surface-raised. Never below 14px.
- **link (#0000ee):** hyperlinks only, always underlined. 8.6:1 on surface.
- **surface (#ffffff) / surface-raised (#f7f7f5):** page and card fills.
  Raised is for the active nav item, muted cards and badges.
- **surface-inverse (#000000):** primary button hover only.
- **border (#e4e4e0):** 1px hairlines. Never used for text.
- **success / warning / danger:** badge text on surface-raised. All three are
  at least 4.5:1 there. Never used as a fill behind white text.
- **focus (#0000ee):** 2px outline with 2px offset on every focusable element.

## Typography

System sans stack, so the page is fully offline with no webfont request.
Scale is Privy's: 42 / 26 / 20 / 18 / 14 / 12. Headings are 600, body 400,
labels 500 with slight tracking. Minimum rendered size for readable text
is 14px; 12px is reserved for uppercase-free labels inside badges.

## Layout

Two-column shell: 240px left nav, fluid main. Below 820px the nav collapses
above the main column and the layout must be one column with no horizontal
overflow. Section spacing uses `spacing.7` (80px) between page blocks and
`spacing.4` (20px) inside cards. No one-off spacing values.

## Elevation

Flat. Depth is shown with `surface-raised` fills and 1px borders, never with
shadows.

## Shapes

`rounded.xs` (8px) for cards, inputs, nav items. `rounded.pill` (100px) for
buttons and badges only.

## Components

- **button-primary:** one per view. Pointer: hover darkens to
  surface-inverse. Keyboard: Enter and Space activate. Touch: minimum
  44x44 hit area. Disabled: 50% opacity, `aria-disabled`, tooltip explains
  why. Loading: label swaps to a verb-ing form and `aria-busy=true`. Error:
  inline text in danger under the control, never a toast alone.
- **button-secondary:** for cancel, filters, "Copy draft". Same states.
- **card / card-muted:** person and job cards. Long names truncate at two
  lines with a `title` attribute carrying the full text. Empty state is a
  card-muted with one sentence and one secondary action.
- **badge-*:** route and verification status. Text is the full word
  ("profile only"), never an abbreviation or an icon alone.
- **nav-item:** `aria-current=page` on the active one, which also gets
  surface-raised.
- **link:** underlined by default, `rel=noopener` when external, visible
  focus ring.

Every interactive element must reach 4.5:1 text contrast in every state
and must show the focus ring when focused via keyboard.

## Do's and Don'ts

- Do use `textContent`; never `innerHTML`.
- Do give every control a descriptive label ("Copy LinkedIn draft", not
  "Copy").
- Do keep drafts unsent: the page has no Send, Apply, Connect or Submit
  control.
- Don't load fonts, scripts or styles from a CDN; the page works offline.
- Don't use colour alone to convey verification status; pair with text.
- Don't invent spacing or type sizes outside the scale above.
