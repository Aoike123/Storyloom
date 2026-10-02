import {RotateCcw} from 'lucide-react';

type Props = {
  busy: boolean;
  running: boolean;
  onRedesign: () => void;
};

export default function AssetRedesign({busy, running, onRedesign}: Props) {
  const reason = busy ? '正在提交，请稍候。' : running ? '本轮任务正在执行，完成后可以重做。' : '已准备好，可以重新生成。';
  return <details className="asset-redesign">
    <summary><RotateCcw size={15}/>需要重新设计整组素材？</summary>
    <div className="asset-redesign-heading">
      <div><h3 id="asset-redesign-title">重做本轮素材</h3>
        <p>整组重做会重新建立身份与服装。仅改衣服时，请在对应服装卡片填写修改意见。</p>
        <p className="asset-redesign-format">人物身份图 ＋ 独立服装图 ＋ 场景图 → 组合分镜</p>
      </div>
      <div className="asset-redesign-action">
        <button type="button" className="button secondary" disabled={busy || running} aria-describedby="asset-redesign-reason" onClick={onRedesign}>
          <RotateCcw size={16}/>重做本轮素材
        </button>
        <small id="asset-redesign-reason">{reason}</small>
      </div>
    </div>
    <p className="asset-redesign-note">重做会重新调用设计和生图模型。旧图片及提示词保留在制作记录中。</p>
  </details>;
}
