import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        cockpit: {
          bg: "#090D16",
          card: "#0E1422",
          surface: "#141C2E",
          hover: "#1A243B",
          border: "#1F2B44",
          borderBright: "#2D3E61",
          text: "#E2E8F0",
          muted: "#8899A6",
          green: "#10B981",
          red: "#F43F5E",
          blue: "#38BDF8",
          amber: "#F59E0B",
          purple: "#A855F7",
        },
      },
      fontFamily: {
        mono: [
          "JetBrains Mono",
          "SF Mono",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
    },
  },
  plugins: [],
};
export default config;
