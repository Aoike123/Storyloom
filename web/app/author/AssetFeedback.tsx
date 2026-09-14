import type {ProgressTask} from '../ProgressFeedback';

type Props={
  name:string;task?:ProgressTask|null;text:string;paid:boolean;busy:boolean;inFlight:boolean;browsingEarlier:boolean;
  onTextChange:(text:string)=>void;onPaidChange:(paid:boolean)=>void;onSubmit:()=>void;
  showPermission?:boolean;
};

export default function AssetFeedback({name,task,text,paid,busy,inFlight,browsingEarlier,onTextChange,onPaidChange,onSubmit,showPermission=true}:Props) {
  const reason=busy?'正在提交修改，请稍候。'
    :browsingEarlier?'当前在回看已完成步骤，请返回当前进度修改。'
    :inFlight?'本轮任务正在执行，结束后可以提交修改。'
    :!task?'素材任务尚未准备好。'
    :!['completed','failed','needs_review'].includes(task.status)?'这份素材当前不能修改，请查看任务状态。'
    :text.trim().length<2?'请填写具体的修改意见（至少 2 个字）。'
    :!paid?(showPermission?'请勾选本卡片的模型调用许可，即可提交修改。':'请先开启本页的模型调用许可。'):'';
  const noteId='feedback-note-'+encodeURIComponent(task?.id||name);
  return <div className="asset-feedback">
    <label>修改意见<textarea aria-label={name+' 修改意见'} maxLength={1500} value={text} onChange={event=>onTextChange(event.target.value)} placeholder="直接描述想改哪里，AI 会整合完整提示词重新生成"/></label>
    {!browsingEarlier&&showPermission&&<label className="checkbox asset-feedback-permission"><input type="checkbox" checked={paid} disabled={busy} onChange={event=>onPaidChange(event.target.checked)}/>允许本作品调用付费模型（当前标签页记住）</label>}
    <button type="button" className="button secondary" disabled={!!reason} title={reason||undefined} aria-describedby={noteId} onClick={()=>{if(!reason)onSubmit();}}>让 AI 按意见修改</button>
    <small id={noteId} className="asset-feedback-note">{reason||'意见会写入完整生成提示词并重新制作；新版本替换当前素材，原版本保留在历史记录中。'}</small>
  </div>;
}
