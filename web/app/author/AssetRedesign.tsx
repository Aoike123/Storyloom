import {RotateCcw} from 'lucide-react';

type Props = {
  paid: boolean;
  busy: boolean;
  running: boolean;
  legacy: boolean;
  onPaidChange: (paid: boolean) => void;
  onRedesign: () => void;
  showPermission?: boolean;
};

export default function AssetRedesign({paid, busy, running, legacy, onPaidChange, onRedesign, showPermission = true}: Props) {
  const reason = busy ? '正在提交，请稍候。' : running ? '本轮任务正在执行，完成后可以重做。' : !paid ? showPermission ? '勾选下方付费调用后即可重做。' : '请先开启本页的模型调用许可。' : '已准备好，可以重新生成。';
  return <section className="asset-redesign" aria-labelledby="asset-redesign-title">
    <div className="asset-redesign-heading">
      <div><h3 id="asset-redesign-title">重做本轮素材</h3>
        <p>{legacy ? '当前素材尚未分离人物身份和服装，需按新工作流重做。' : '整组重做会重新建立身份与服装。仅改衣服时，请在对应服装卡片填写修改意见。'}</p>
        <p className="asset-redesign-format">人物身份图 ＋ 独立服装图 ＋ 场景图 → 组合分镜</p>
      </div>
      <div className="asset-redesign-action">
        <button type="button" className="button primary" disabled={busy || running || !paid} aria-describedby="asset-redesign-reason" onClick={onRedesign}>
          <RotateCcw size={16}/>重做本轮素材
        </button>
        <small id="asset-redesign-reason">{reason}</small>
      </div>
    </div>
    {showPermission && <label className="checkbox"><input type="checkbox" checked={paid} disabled={busy || running} onChange={event => onPaidChange(event.target.checked)}/>允许本次操作调用付费模型</label>}
    <p className="asset-redesign-note">重做会重新调用设计和生图模型。旧图片及提示词保留在制作记录中。</p>
  </section>;
}
