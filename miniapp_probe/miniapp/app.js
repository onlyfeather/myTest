const probe = require('./utils/probe.js');

App({
  onLaunch() {
    probe.start();
  },
  onShow() {
    probe.log('小程序切到前台');
  },
  onHide() {
    probe.log('小程序切到后台');
  },
});
