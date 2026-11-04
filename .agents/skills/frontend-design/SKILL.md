---
name: frontend-design
description: Guidance for distinctive, intentional visual design when building new UI or reshaping an existing one. Helps with aesthetic direction, typography, and making choices that don't read as templated defaults.
license: Apache-2.0
---

# Frontend Design

Approach this as the design lead at a studio known for giving every client a distinct visual identity. Make deliberate, opinionated choices about palette, typography, layout, and interaction that are specific to the product. Avoid generic AI-generated interface patterns.

## Ground the design in the product

Before coding, identify:
- The product's purpose and primary user.
- The user's main task on this screen.
- The information hierarchy and the decisions the interface must support.
- The product's domain conventions and real content.

Use the existing product, audience, and codebase as evidence. Do not invent data, capabilities, or user needs.

## Design principles

- Typography carries personality. Choose typefaces and a clear scale intentionally; avoid default font pairings and arbitrary weights.
- Use color semantically: distinguish background, surfaces, borders, primary actions, secondary information, and status states.
- Layout is information architecture. Alignment, grouping, spacing, and density should communicate relationships rather than decorate.
- Use hierarchy deliberately. Not every item deserves a card, badge, border, glow, or shadow.
- Use real product language. Labels should be clear and consistent; actions should say exactly what happens.
- Motion should communicate a state change or guide attention. Avoid scattered entrance animations and gratuitous hover effects.
- Design for responsive layouts, keyboard operation, visible focus, reduced motion, and sufficient contrast from the start.

## Avoid generic AI aesthetics

Do not default to:
- Identical rounded cards for every content group.
- Purple/blue gradients, glowing borders, glassmorphism, or decorative blobs without a product-specific reason.
- Repeated uppercase eyebrow labels and excessive micro-labels.
- A large hero headline where the task requires a dense working dashboard.
- Fake metrics, placeholder charts, decorative icons, or invented activity.
- The same spacing, radius, and shadow on every component.
- Emojis as interface icons when a coherent icon set is available.
- Unnecessary motion on every card, section, or control.

These patterns are not forbidden when the brief justifies them; they must be intentional choices, not defaults.

## Plan before implementation

First produce a compact design plan:
1. Product/audience and primary task.
2. Visual direction and why it fits this product.
3. Named color tokens (4–6 core colors) with semantic roles.
4. Typography roles and scale.
5. Layout concept and content hierarchy.
6. One distinctive visual signature, if useful.
7. Responsive and interaction behavior.

Compare the plan against generic templates. Revise any choice that could be pasted unchanged into an unrelated SaaS dashboard.

## Build and critique

- Inspect existing components and preserve working behavior unless the task explicitly changes it.
- Build reusable tokens and components instead of one-off styling.
- Use real content and realistic data density.
- Check loading, empty, error, success, and disabled states.
- Review desktop and mobile layouts, keyboard focus, contrast, and reduced-motion behavior.
- If screenshots or a browser preview are available, inspect the rendered result and correct visual problems before delivery.
- Remove decoration that does not improve comprehension, identity, or task completion.

## Dashboard and data-product guidance

For analytical products:
- Prioritize scanability, comparison, context, and decision support over marketing-style presentation.
- Establish a clear hierarchy between primary market state, key metrics, charts, signals, risk, and supporting detail.
- Give charts meaningful axes, units, time ranges, legends, and tooltips. Never imply certainty or fabricate data.
- Use consistent semantic colors for positive, negative, warning, neutral, and unavailable states; do not rely on color alone.
- Make timestamps, data freshness, source, and calculation context visible where they affect interpretation.
- Keep dense data readable; use progressive disclosure instead of hiding essential context.

## Writing in interfaces

Use plain language, active voice, sentence case, and consistent terminology. A button should describe its outcome (for example, "Save changes"). Errors should state what happened and how to recover. Empty states should explain the next useful action.