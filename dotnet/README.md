# aztree

**Where did my Azure money go?** aztree reads your Azure costs and draws them as a treemap: big box, big cost.

```bash
dotnet tool install -g aztree
aztree --demo      # try it with fake data, no Azure needed
az login
aztree             # your current subscription
aztree --all       # every subscription you can see
```

It writes one HTML page that works offline. It needs .NET 8 or later, and the Azure CLI (`az login`) or a token in `AZURE_ACCESS_TOKEN`.

This package carries aztree built for Windows (x64), Linux (x64) and macOS (Apple silicon and Intel). No Python needed. Documentation, options and source: https://github.com/milanm/aztree
