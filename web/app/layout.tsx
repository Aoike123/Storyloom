import type { Metadata } from 'next';
import './globals.css';
import './extra.css';
export const metadata: Metadata = {title:'叙间 · 走进故事的另一种可能', description:'让故事回应你的选择'};
export default function RootLayout({children}: Readonly<{children:React.ReactNode}>) {return <html lang="zh-CN"><body>{children}</body></html>;}
