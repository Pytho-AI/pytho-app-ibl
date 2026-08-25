/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html",
    "./src/**/*.{js,ts,jsx,tsx}", "./src/subframe/**/*.{tsx,ts,js,jsx}"],
  darkMode: 'class',
  theme: {
    extend: {},
  },
  plugins: [],
  presets: [require("./src/subframe/tailwind.config.js")]
};

