// The left half of the login and sign-up pages.
export default function AuthBrand() {
  return (
    <aside className="auth-brand">
      <div className="brand">
        <span className="brand-mark">A</span>
        AILab
      </div>
      <div>
        <h2>在隔离沙箱里，把任务交给 Agent</h2>
        <p>描述你要做的事，Agent 会在受限的沙箱里写代码、跑命令、生成文件，产出自动归档到你的资料库。</p>
      </div>
      <ul className="auth-points">
        <li>沙箱默认无网络，代码与数据互相隔离</li>
        <li>模型由平台统一接入，无需自带 API Key</li>
        <li>用量按模型倍率折算，每日 / 每月额度一目了然</li>
      </ul>
    </aside>
  );
}
