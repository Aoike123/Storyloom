'use client';

import type {Release} from './reader-types';

type Props = {
  creator?: Release['creator'];
  className?: string;
};

export default function ReaderCreatorAvatar({creator, className = ''}: Props) {
  const name = creator?.name?.trim() || '创作者';
  const initial = Array.from(name)[0] || '创';
  return <span className={'reader-creator-avatar ' + className} title={'制作者：' + name} aria-label={'制作者：' + name}>
    <span aria-hidden="true">{initial}</span>
    {creator?.avatar_path && (
      <img src={creator.avatar_path} alt="" aria-hidden="true"
        onError={event => { event.currentTarget.hidden = true; }}/>
    )}
  </span>;
}
