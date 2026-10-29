/** The color roles and the brand's preset themes (Admin → Theme). The built-in colors in
 *  index.css are the Crypt preset; the server keeps the role names in app/api/theme.py. */

export const ROLES = [
  "bg", "surface", "surface-raised", "surface-hover", "border", "border-strong",
  "text", "text-muted", "text-subtle", "accent", "accent-hover", "accent-fg",
  "primary", "primary-hover", "primary-fg", "link", "focus",
  "success", "success-bg", "warning", "warning-bg", "danger", "danger-bg", "info", "info-bg",
  "overlay", "danger-solid", "danger-solid-hover", "danger-solid-fg", "danger-solid-border",
] as const;
export type Role = (typeof ROLES)[number];
export type Colors = Record<Role, string>;

/** How the editor groups the roles, with what each one colors. */
export const ROLE_GROUPS: { title: string; roles: [Role, string][] }[] = [
  { title: "Surfaces", roles: [
    ["bg", "Page background"], ["surface", "Cards, inputs, menus"], ["surface-raised", "Chips, selected rows, popovers"],
    ["surface-hover", "Hovered chips and rows"], ["border", "Dividers and card edges"], ["border-strong", "Input and button outlines"],
  ] },
  { title: "Text", roles: [
    ["text", "Main text"], ["text-muted", "Secondary text, labels"], ["text-subtle", "Placeholders, timestamps"],
    ["link", "Links and secondary-button labels"],
  ] },
  { title: "Accent", roles: [
    ["accent", "Icons, progress, active markers, logo"], ["accent-hover", "Accent on hover"], ["accent-fg", "Text and icons on accent"],
    ["focus", "Keyboard focus outline"],
  ] },
  { title: "Primary button", roles: [
    ["primary", "Fill"], ["primary-hover", "Fill on hover"], ["primary-fg", "Label"],
  ] },
  { title: "Delete buttons", roles: [
    ["danger-solid", "Fill"], ["danger-solid-hover", "Fill on hover"], ["danger-solid-fg", "Label"],
    ["danger-solid-border", "Edge"],
  ] },
  { title: "Status", roles: [
    ["success", "Success text"], ["success-bg", "Success background"], ["warning", "Warning text, ratings"],
    ["warning-bg", "Warning background"], ["danger", "Error text"], ["danger-bg", "Error background"],
    ["info", "Info text"], ["info-bg", "Info background"],
  ] },
  { title: "Overlay", roles: [["overlay", "Behind dialogs, the lightbox and video controls"]] },
];

const DANGER_SOLID = {
  "danger-solid": "#4A0D17", "danger-solid-hover": "#6E1423", "danger-solid-fg": "#FFFFFF", "danger-solid-border": "#D14B55",
} as const;

export const PRESETS: { id: string; name: string; note: string; colors: Colors }[] = [
  { id: "crypt", name: "Crypt", note: "The default", colors: {
    bg: "#0B0B10", surface: "#1E2230", "surface-raised": "#262B3B", "surface-hover": "#2A2F42",
    border: "#2A2F40", "border-strong": "#747A90", text: "#E6E6F0", "text-muted": "#9AA0B4", "text-subtle": "#8C92A7",
    accent: "#9164CA", "accent-hover": "#A47AD6", "accent-fg": "#0B0B10",
    primary: "#2E1949", "primary-hover": "#3A2160", "primary-fg": "#FFFFFF", link: "#9E77D0", focus: "#9164CA",
    success: "#A3E635", "success-bg": "#1A2410", warning: "#F2B84B", "warning-bg": "#2A2112",
    danger: "#F87171", "danger-bg": "#2C1416", info: "#7DB4F5", "info-bg": "#121E2E",
    overlay: "rgba(5, 5, 8, 0.72)", ...DANGER_SOLID,
  } },
  { id: "crypt-lime", name: "Crypt Lime", note: "Cyber-lime accent", colors: {
    bg: "#0B0B10", surface: "#1E2230", "surface-raised": "#262B3B", "surface-hover": "#2A2F42",
    border: "#2A2F40", "border-strong": "#747A90", text: "#E6E6F0", "text-muted": "#9AA0B4", "text-subtle": "#8C92A7",
    accent: "#A3E635", "accent-hover": "#B8F05A", "accent-fg": "#0B0B10",
    primary: "#24310F", "primary-hover": "#2E3E14", "primary-fg": "#FFFFFF", link: "#B8F05A", focus: "#A3E635",
    success: "#4ADE80", "success-bg": "#112418", warning: "#F2B84B", "warning-bg": "#2A2112",
    danger: "#F87171", "danger-bg": "#2C1416", info: "#7DB4F5", "info-bg": "#121E2E",
    overlay: "rgba(5, 5, 8, 0.72)", ...DANGER_SOLID,
  } },
  { id: "ossuary", name: "Ossuary", note: "True black, for OLED screens", colors: {
    bg: "#000000", surface: "#141414", "surface-raised": "#1C1C1C", "surface-hover": "#222222",
    border: "#2A2A2A", "border-strong": "#6E6E6E", text: "#EDEDED", "text-muted": "#A0A0A0", "text-subtle": "#8E8E8E",
    accent: "#9164CA", "accent-hover": "#A47AD6", "accent-fg": "#000000",
    primary: "#2E1949", "primary-hover": "#3A2160", "primary-fg": "#FFFFFF", link: "#A47AD6", focus: "#9164CA",
    success: "#A3E635", "success-bg": "#16200C", warning: "#F2B84B", "warning-bg": "#261D0E",
    danger: "#F87171", "danger-bg": "#281113", info: "#7DB4F5", "info-bg": "#0F1A28",
    overlay: "rgba(0, 0, 0, 0.78)", ...DANGER_SOLID,
  } },
];
