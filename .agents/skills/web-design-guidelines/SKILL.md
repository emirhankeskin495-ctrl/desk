---
name: web-design-guidelines
description: Audit web UI implementation for accessibility, responsive behavior, interaction quality, performance, and consistency. Use when reviewing or polishing existing pages and components.
metadata:
  author: vercel
  version: "1.0.0"
---

# Web Interface Guidelines

Use this skill to audit implemented UI, not to invent a new visual direction.

## Review process

1. Inspect the target files and understand the page's purpose and user flows.
2. Fetch the latest Vercel Web Interface Guidelines from:
   https://raw.githubusercontent.com/vercel-labs/web-interface-guidelines/main/command.md
3. Review the implementation against the current rules. If the remote guidelines cannot be fetched, say so and use the checklist below without claiming a full Vercel audit.
4. Report concrete findings with file and line references where possible.
5. Separate blockers from improvements. Do not rewrite unrelated code.

## Baseline checklist

### Accessibility
- Semantic HTML and correct heading hierarchy.
- Keyboard access for every interactive control.
- Visible focus indicators.
- Accessible names for icon-only controls.
- Sufficient text and non-text contrast.
- Form labels, helpful errors, and appropriate ARIA only where native semantics are insufficient.

### Responsive behavior
- No horizontal overflow at narrow widths.
- Content hierarchy remains clear on mobile.
- Touch targets are comfortably usable.
- No clipped dialogs, menus, charts, or tables.
- Respect safe areas where relevant.

### Interaction and states
- Buttons and links have clear, distinct purposes.
- Hover is never the only interaction cue.
- Loading, empty, error, success, disabled, and selected states are designed.
- Destructive or consequential actions have appropriate confirmation and recovery.
- Motion respects prefers-reduced-motion.

### Visual consistency
- Typography, spacing, colors, radii, and elevation use coherent tokens.
- Status colors are consistent and not the sole carrier of meaning.
- Components are not wrapped in cards or badges without an information-hierarchy reason.
- Charts include units, time context, legends, and accessible labels where relevant.

### Performance
- Images have appropriate dimensions and loading behavior.
- Layout shifts are avoided.
- Animations are restrained and performant.
- Avoid unnecessary client-side work and oversized assets.

## Output format

For each finding provide:
- Severity: blocker, high, medium, or low.
- File and line (if available).
- What is wrong and why it matters.
- A specific correction.

Finish with a short summary of the highest-impact fixes. Do not claim a page was visually verified unless you inspected a rendered preview or screenshot.