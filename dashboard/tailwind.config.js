export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        soc: {
          bg: "#0b0f17",
          panel: "#111723",
          panelSoft: "#171f2e",
          border: "#252f42",
          text: "#e8edf5",
          muted: "#8b95a8",
          electric: "#4f8cff",
          red: "#f26b6b",
          amber: "#f0b44c",
          green: "#3fbf8c",
        },
      },
      boxShadow: {
        panel: "0 8px 24px -12px rgba(0,0,0,0.6)",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["'IBM Plex Mono'", "ui-monospace", "monospace"],
        display: ["'Playfair Display'", "Georgia", "serif"],
      },
    },
  },
  plugins: [],
};
