# mihomo：让 WebUI 能切换节点、更新订阅

WebUI「网络代理」页下面的「节点和订阅」通过 mihomo 的控制接口（external-controller）工作：
看代理组和节点、测速、切换节点、立即更新订阅。它不会改 mihomo 的配置文件。

mihomo 的配置文件在服务器上的 `~/myTest/mihomo/config.yaml`，不在 git 仓库里，`git pull` 不会改它。下面的修改要在服务器上手动做。

## 1. 先备份

```bash
cp ~/myTest/mihomo/config.yaml ~/myTest/mihomo/config.yaml.bak-$(date +%Y%m%d)
```

## 2. 打开控制接口（只能切换节点的话，做这一步就够）

把配置里的

```yaml
external-controller: '127.0.0.1:9090'
```

改成下面两行。`secret` 换成一串随机字符，可以用 `openssl rand -hex 16` 生成：

```yaml
external-controller: '0.0.0.0:9090'
secret: '换成你生成的随机字符'
```

`127.0.0.1` 只有 mihomo 自己能连，所以 NoneBot 连不上；改成 `0.0.0.0` 之后，同一个 compose 网络里的容器都能连。
mihomo 没有把 9090 映射到宿主机（`docker ps` 里它的端口那一栏是空的），外网连不到。**不要**在 docker-compose.yml 里给它加 `ports: 9090`。

## 3. 把订阅写进配置（要在 WebUI 里更新订阅才需要）

现在的配置是把节点直接写在 `proxies:` 里，mihomo 不知道订阅地址，没法更新。
改成 `proxy-providers`：mihomo 自己定时拉订阅，WebUI 也能点「立即更新」。

下面是改好的结构，只列出要动的部分。`dns`、`rules` 这些照你原来的保留：

```yaml
proxy-providers:
  机场A:                      # 名字随便起，会显示在 WebUI 里
    type: http
    url: '你的订阅地址'         # Clash / mihomo 格式的订阅
    path: ./providers/airport-a.yaml
    interval: 21600            # 每 6 小时自动更新一次
    health-check:
      enable: true
      url: https://www.gstatic.com/generate_204
      interval: 600
  # 有第二个订阅就照着再写一个
  # 机场B:
  #   type: http
  #   url: '另一个订阅地址'
  #   path: ./providers/airport-b.yaml
  #   interval: 21600

proxy-groups:
  - name: 节点选择             # 手动选择的组，WebUI 里可以切换
    type: select
    proxies: [自动选择, 机场A, DIRECT]
  - name: 自动选择             # 自动选最快的节点
    type: url-test
    use: [机场A]
    url: https://www.gstatic.com/generate_204
    interval: 300
  - name: 机场A                # 想手动挑机场A里的某个节点，就在这个组里切
    type: select
    use: [机场A]

rules:
  # 原来的规则保留，最后一条让其余流量走「节点选择」
  - MATCH,节点选择
```

- 原来 `proxies:` 里写死的节点可以删掉，也可以留着，然后在 `proxies: [...]` 里列出它们的名字。
- 订阅地址等于账号密码，不要发给别人，也不要提交到 git。
- 有几个订阅就写几个 provider。在「节点选择」里选哪个机场的组，就是在订阅之间切换。

## 4. 重启 mihomo，在 WebUI 里填上

```bash
cd ~/myTest
docker compose restart mihomo
docker logs --tail 30 mytest-mihomo
```

日志里没有 `error` 就行。如果启动失败，所有走代理的服务都会连不上，这时用第 1 步的备份恢复后再重启。

然后在 WebUI「网络代理」页：

- mihomo 控制接口：`http://mihomo:9090`（`mihomo` 是 docker-compose.yml 里的服务名）
- 控制接口密钥：和上面的 `secret` 一样
- 公共代理：`http://mihomo:7890`（对应配置里的 `mixed-port`）

保存后点右上角「测试连接」，第一行显示「mihomo 控制接口：连上了」就好了。页面下方会列出代理组和订阅。

## 注意

- 在 WebUI 里切换的节点，mihomo 重启后会回到默认节点（配置里每个组的第一项）。
  想记住选择，在配置里加上：
  ```yaml
  profile:
    store-selected: true
  ```
- 「自动选择」这类 `url-test` 组由 mihomo 按延迟自己选节点，WebUI 里不能手动切换，只能测速。
