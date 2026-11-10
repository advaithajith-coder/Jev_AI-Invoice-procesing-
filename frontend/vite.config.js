import { defineConfig } from 'vite';

export default defineConfig({
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: '../src/jev_invoice/web/static/react',
    emptyOutDir: true,
    lib: { entry: 'src/main.jsx', formats: ['es'], fileName: () => 'app.js', cssFileName: 'design' },
  },
});
