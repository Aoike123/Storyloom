'use client';

import Link, {useLinkStatus} from 'next/link';
import type {ComponentProps} from 'react';
import {LoaderCircle} from 'lucide-react';

function PendingStatus() {
  const {pending} = useLinkStatus();
  return pending ? <span className="reader-link-pending" role="status"><LoaderCircle size={14} className="feedback-spin"/>正在打开…</span> : null;
}

export default function ReaderLink({children, ...props}: ComponentProps<typeof Link>) {
  return <Link {...props}>{children}<PendingStatus/></Link>;
}
