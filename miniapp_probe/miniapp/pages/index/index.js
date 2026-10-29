const probe = require('../../utils/probe.js');

Page({
  data: { logs: [], client: '' },

  onLoad() {
    this.setData({ client: probe.client(), logs: probe.logs().slice(0, 80) });
    this.unsubscribe = probe.subscribe((logs) => {
      // 后台时 setData 没有意义，回到前台再刷新。
      if (!this.hidden) this.setData({ logs: logs.slice(0, 80) });
    });
  },
  onShow() {
    this.hidden = false;
    this.setData({ logs: probe.logs().slice(0, 80) });
  },
  onHide() { this.hidden = true; },
  onUnload() { if (this.unsubscribe) this.unsubscribe(); },

  runDirect() { probe.runDirect(); },
  runFetch() { probe.runFetch(); },
  runRemote() { probe.runRemote(); },
  stop() { probe.stop(); },
});
