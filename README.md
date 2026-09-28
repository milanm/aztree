# aztree

**Where did my Azure money go?** aztree reads your Azure costs and draws them as a treemap: big box, big cost. It's a port of [awstree](https://github.com/petricbranko/awstree) by Branko Petric, which took the idea from [disktree](https://x.com/tobi/status/2103251521223921739) by [Tobi Lütke](https://x.com/tobi). Same idea again, pointed at an Azure subscription.

![aztree showing a demo Azure bill as a treemap](https://raw.githubusercontent.com/milanm/aztree/main/docs/screenshot.png)

It's one small Python package with no dependencies, and it produces a single HTML page that works offline.

## Quick start

```bash
pipx install aztree         # or: pip install aztree
aztree --demo               # try it with fake data, no Azure needed
az login
aztree                      # your current subscription
aztree --all                # every subscription you can see
```

From a clone, run `python -m aztree` without installing anything.

The page opens in your browser. It shows the last 30 days, compared with the 30 days before. The last day is the day before yesterday, because Azure is still posting yesterday's usage.

## What you get

- **Treemap** of spend by service → meter. Press `2` to group by subscription, `3` by region, `4` by resource group → resource and `5` by tag value → service.
- **Tag view:** spend by the values of one tag, with untagged spend as its own box and its share of the bill in the header. aztree picks the tag on the most resources; `--tag KEY` picks another.
- **Color by change** (`c`): red where spend grew, green where it shrank.
- **Selection panel:** cost, share of the bill, change vs the previous period, monthly pace and a daily chart. For the whole bill, the last two cells show this month so far and Azure's forecast for the month.
- **Worth a look:** one list where news and to-dos take turns. News is costs that grew and one-off spikes (a day far above that meter's usual). To-dos are known money pits, compute left on all week in dev/test resource groups, and resources that bill while doing nothing, which aztree finds with Azure Resource Graph (VMs stopped but not deallocated, unattached disks, unused public IPs, snapshots older than 90 days, App Service plans with no apps, NAT gateways on no subnet). When Advisor has no reservation or savings-plan tips, steady spend a reservation could cut shows up too. The first six show; "show all" opens the rest.
- **Money pits it knows:** Log Analytics ingestion, data transfer out, NAT and Firewall processing, public IPs, snapshots, retiring VM series with their retirement dates, Standard and Premium v2 App Service plans, several Front Door profiles, Front Door Premium, large DTU databases, Azure DevOps seats and hosted jobs, private endpoints, provisioned Cosmos DB throughput, 1-minute alert rules, Azure Cache for Redis (retiring) and Extended Security Updates.
- **Biggest drops:** what fell since the previous period, including anything that went to zero, so you can see a saving land.
- **Advisor:** Azure Advisor's cost recommendations (reservations, savings plans, right-sizing), one per resource and SKU. Reservation and savings-plan tips list the meters they would cover and what those run at a month. Click a tip to jump to its biggest meter, or to the resource it's about.
- **Export for AI:** a JSON summary to hand to any AI agent (see below).

Click to select, double-click to zoom in, `⌫` to go back up, `/` to filter. Add `#resource`, `#subscription`, `#region` or `#tag` to the page's URL to open that view.

## Requirements

- Python 3.9+ and the [Azure CLI](https://aka.ms/azcli), logged in with `az login`. Without the CLI, put a token for `https://management.azure.com/` in `AZURE_ACCESS_TOKEN` and name the subscriptions with `--subscription` or `--all`. A bare token only sees its own tenant; through the CLI, aztree sees every tenant you're logged in to and gets a token for each.
- The **Cost Management Reader** role (or Reader) on each subscription. Advisor tips and the Resource Graph checks need Reader; without it you still get the page.
- Tested on a CSP subscription. On CSP, Azure shows costs at retail prices without your partner's discounts, and reserved usage as $0 even with `--metric AmortizedCost`, so the totals won't match your partner's invoice. Pay-as-you-go and Visual Studio subscriptions use the same API.
- EA and MCA billing scopes should work through `--scope`, but nobody has tried yet. At such a scope the subscription view shows the whole scope as one box, and Advisor is skipped.

**Cost:** Cost Management queries are free. Azure throttles them per subscription and per tenant, so a run makes about 10–14 requests per subscription and may wait 30–60 seconds when Azure asks it to. aztree saves the data in `~/.aztree/`, so reopening the page is instant:

```bash
aztree --from
```

## Ask an AI about your bill

```bash
aztree --from --export
```

This writes `~/.aztree/aztree-export.json`, a compact summary that includes instructions for the agent. It has totals, breakdowns by service, subscription, region and resource group, and every meter with its change vs the previous period. It also lists the top growers and drops, credits and refunds, the flagged money pits, idle resources, the tag breakdown, this month's forecast and the Advisor tips. Give it to an AI agent and ask *"where can I save money?"*. The **Export for AI** button in the page (or `e`) downloads the same file.

## Options

| Flag | What it does |
|---|---|
| `--demo` | Fake data, no Azure access needed |
| `--subscription ID_OR_NAME` | Subscription to read; repeat for more. Default: the Azure CLI's current one |
| `--all` | Every enabled subscription you can see, in every tenant you're logged in to |
| `--scope SCOPE` | Any Cost Management scope, e.g. `/providers/Microsoft.Billing/billingAccounts/ID` (untested) |
| `--days N` | Period length, default 30. It's always compared with the period before it. |
| `--metric M` | `ActualCost` (default) or `AmortizedCost` |
| `--no-advisor` | Skip Azure Advisor |
| `--tag KEY` | Tag for the tag view. Default: the tag on the most resources |
| `--no-graph` | Skip the Resource Graph checks for idle resources |
| `--from [FILE]` | Reopen saved data without calling Azure (default: the last run) |
| `--export [FILE]` | Write the AI summary JSON instead of the page (default `~/.aztree/aztree-export.json`) |
| `--out FILE` | Where to write the page (default `~/.aztree/aztree.html`) |
| `--no-open` | Don't open the browser |
| `--verbose` | Print pages, rows and query units per request |
| `--version` | Print the version |

## Good to know

- `ActualCost` books a reservation or savings plan purchase as one lump on the day you bought it. `--metric AmortizedCost` spreads it over the term.
- Credits and refunds count in the totals, but negative amounts can't be drawn as boxes. The header says how much of the total they are.
- Subscriptions that bill in different currencies are drawn in USD.
- If a subscription has so many resources that a daily breakdown takes more than ten pages of results, the resource view still lists every resource but with one total per period and no daily chart.
- Marketplace charges keep their publisher's meter names and get no special handling.
- The forecast is Azure's own, for the calendar month, so it doesn't follow `--days`. It's left out when a subscription has none, or when it comes in a different currency from the page.
- Runs saved by older versions open without the tag view, the forecast and the idle checks.
- Runs save to `~/.aztree/`; set `AZTREE_HOME` to move it. Older versions run from a clone saved to `out/`, and `aztree --from out/aztree-data.json` still opens those runs.

## Checking against the portal

In the Azure portal, open **Cost Management → Cost analysis**, choose the cost type you ran with (**Actual cost** by default), daily granularity and the dates in aztree's header. The totals should match to the cent. Azure can still add a little to the last day after aztree reads it, so compare soon after a run.

## Privacy

aztree only reads cost data, forecasts, tag names, Advisor recommendations and Resource Graph properties, and never changes anything in your subscriptions. Everything it writes goes to `~/.aztree/` (or `AZTREE_HOME`), outside any git repo, because it contains your subscription IDs and costs. The page loads nothing from the internet. Share the HTML or export file only with people who should see your bill.

## Development

```bash
python -m unittest discover -s tests
python -m aztree --demo
```

The tests need no Azure access. The viewer tests run the page's script in Node and are skipped without it.

## Credits

aztree is a port of [awstree](https://github.com/petricbranko/awstree) by Branko Petric (MIT). It keeps awstree's viewer and its JSON contract and replaces the AWS parts. awstree, in turn, is inspired by [disktree](https://x.com/tobi/status/2103251521223921739) by [Tobi Lütke](https://x.com/tobi).

aztree is not affiliated with or endorsed by Microsoft.

## License

[MIT](LICENSE)
