import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwind from "@tailwindcss/vite";
export default defineConfig({ plugins:[react(),tailwind()], build:{outDir:"../src/kong/local_api/static",emptyOutDir:true},server:{proxy:{"/v1":"http://127.0.0.1:8765"}},test:{environment:"jsdom"}});
