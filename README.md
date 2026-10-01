# aztree

[![PyPI](https://img.shields.io/pypi/v/aztree?label=PyPI)](https://pypi.org/project/aztree/) [![NuGet](https://img.shields.io/nuget/v/aztree?label=NuGet)](https://www.nuget.org/packages/aztree)

aztree shows where your Azure money went. It draws your costs as a treemap, where a bigger box means a bigger cost.

**See it live on demo data:** [milanm.github.io/aztree](https://milanm.github.io/aztree/)

![aztree showing a demo Azure bill as a treemap](https://raw.githubusercontent.com/milanm/aztree/main/docs/screenshot.png)

It's one small Python package with no dependencies. It writes a single HTML page that works offline.

## Quick start

Install it with Python, .NET or Homebrew:

```bash
pipx install aztree              # Python 3.9+ (or: pip install aztree)
dotnet tool install -g aztree    # .NET 8 or later, no Python needed
brew install milanm/tap/aztree   # macOS and Linux, no Python needed
```

Or download the executable for Windows, Linux or macOS from the [latest release](https://github.com/milanm/aztree/releases/latest). It doesn't need Python. The downloads aren't signed yet, so Windows and macOS ask once before running them. On Linux and macOS, run `chmod +x` on the file first.

A few antivirus engines, Microsoft Defender among them, flag the Windows executable. It's a false positive that hits many Python tools packed with PyInstaller, and it has been reported to Microsoft. Releases after 0.9.2 prove where their executables came from: `gh attestation verify aztree-win-x64.exe --repo milanm/aztree` checks that this repository's release workflow built the file from the tagged commit. If you'd rather not run an executable at all, `pipx install aztree` runs the plain Python source.

Then:

```bash
aztree --demo                    # try it with fake data, no Azure needed
az login
aztree                           # your current subscription
aztree --all                     # every subscription you can see
```

From a clone, run `python -m aztree` without installing anything.

The page opens in your browser. It shows the last 30 days, compared with the 30 days before. The last day is the day before yesterday, because Azure is still posting yesterday's usage.

## What you get

- **Treemap** of spend by service → meter. Press `2` to group by subscription, `3` by region, `4` by resource group → resource and `5` by tag value → service.
- **Tag view:** spend by the values of one tag, with untagged spend as its own box and its share of the bill in the header. aztree picks the tag on the most resources; `--tag KEY` picks another. When that share is a tenth of the bill or more, "how to fix" there points to tag inheritance and a tag policy.
- **Color by change** (`c`): red where spend grew, green where it shrank.
- **Selection panel:** cost, share of the bill, change vs the previous period, monthly pace and a daily chart. For the whole bill, the last two cells show this month so far and Azure's forecast for the month. The name of a selected subscription, resource group or resource opens it in the Azure portal.
- **Worth a look:** one list where news and to-dos take turns. The first six show, and "show all" opens the rest. On the map, the boxes a to-do is about have diagonal stripes.
  - **News** is costs that grew and one-off spikes (a day far above that meter's usual).
  - **To-dos** are known money pits (below) and compute left on all week in dev/test resource groups. When Advisor has no reservation or savings-plan tips, steady spend that a reservation could cut shows up too.
  - **Idle resources** bill while doing nothing, and they count as to-dos. aztree finds them with Azure Resource Graph and lists the ones that cost something in the period. When one check finds several, such as 3 unattached disks, click the row to list them all, each with its cost and a link to the portal. Click a resource in the list to see it on the map.
    - Compute and storage: VMs stopped but not deallocated, unattached disks, Premium disks of VMs deallocated for over 30 days, and snapshots older than 90 days or on Premium storage.
    - Networking: unused public IPs, NAT gateways on no subnet, and VPN or ExpressRoute gateways with no connections. Also Application Gateways and load balancers with no backends, ExpressRoute circuits the provider hasn't set up, and disconnected private endpoints.
    - Apps and databases: empty App Service plans and SQL elastic pools with no databases.
- **Money pits it knows:**
  - Compute: retiring VM series with their retirement dates, Extended Security Updates, and Standard and Premium v2 App Service plans.
  - Networking: data transfer out, NAT and Firewall processing, public IPs, private endpoints, several Front Door profiles and Front Door Premium.
  - Storage and databases: snapshots, large DTU databases, provisioned Cosmos DB throughput and Azure Cache for Redis (retiring).
  - Monitoring: Log Analytics ingestion and 1-minute alert rules.
  - Azure DevOps: seats and hosted jobs.
- **Biggest drops:** what fell since the previous period, including anything that went to zero. Use it to check that a saving worked.
- **Advisor:** Azure Advisor's cost tips for reservations, savings plans and right-sizing, one per resource and SKU. Reservation and savings-plan tips show related spend: the meters of the kind they commit to, and what those cost a month. aztree matches them by service and meter, not by SKU or region, so a reservation covers less than that. Click a tip to jump to its biggest meter, or to the resource it's about.
- **Export for AI:** a JSON summary to hand to any AI agent (see below).

On the map:

- Click to select, and click again or press Enter to zoom in. Click a "+N more" box twice to see the small items it holds.
- Once you've clicked the map, `Tab` moves to the next box by size.
- `⌫` goes back up. The browser's Back button undoes zooms and jumps.
- `/` opens the filter. What doesn't match fades, and Enter keeps only the matches.
- `Esc` clears the filter, then the selection.
- Add `#resource`, `#subscription`, `#region` or `#tag` to the page's URL to open that view.

## Requirements

- The [Azure CLI](https://aka.ms/azcli), logged in with `az login`, and Python 3.9+ or .NET 8+ unless you use the downloaded executable.
- Without the CLI, put a token for `https://management.azure.com/` in `AZURE_ACCESS_TOKEN`. Then name the subscriptions with `--subscription` or `--all`. A token only sees its own tenant. Through the CLI, aztree gets a token for every tenant you're logged in to.
- If you use Azure PowerShell instead of the CLI, give aztree a token and name the subscription:

  ```powershell
  $t = (Get-AzAccessToken -ResourceUrl https://management.azure.com/).Token
  $env:AZURE_ACCESS_TOKEN = if ($t -is [securestring]) { [System.Net.NetworkCredential]::new('', $t).Password } else { $t }
  aztree --subscription "My subscription"
  ```
- The **Cost Management Reader** role (or Reader) on each subscription. Advisor tips, the Resource Graph checks and the automatic tag choice need Reader. Without it you still get the page, and `--tag KEY` still gives you the tag view.
- Tested on a CSP subscription, where Azure shows retail prices without your partner's discounts. It also shows reserved usage as $0, even with `--metric AmortizedCost`, so the totals won't match your partner's invoice. Pay-as-you-go and Visual Studio subscriptions use the same API.
- EA and MCA billing scopes should work through `--scope`, but nobody has tried yet. At such a scope the subscription view shows the whole scope as one box, and Advisor is skipped.
- Behind a proxy that inspects HTTPS, aztree needs your company's root certificate, like the Azure CLI does. Set `SSL_CERT_FILE` (or `REQUESTS_CA_BUNDLE`, which the CLI reads) to a PEM file that holds it.

**Cost:** Cost Management queries are free, but Azure limits how often you can call them, per subscription and per tenant. A run makes about 10–14 requests per subscription and may wait 30–60 seconds when Azure asks it to.

aztree saves the data in `~/.aztree/`, so reopening the page is instant:

```bash
aztree --from
```

Each run also keeps a dated copy in `~/.aztree/history/`, the last 12, so a later version can say what changed since. Like the saved data, the copies hold your costs and subscription IDs. Delete the folder whenever you like.

## Ask an AI about your bill

```bash
aztree --from --export
```

This writes `~/.aztree/aztree-export.json`, a summary of your bill with instructions for the agent. The **Export for AI** button in the page (or `e`) downloads the same file.

The summary has totals and every meter with its change vs the previous period. It breaks the bill down by service, subscription, region, resource group and tag. It also lists the top growers and drops, credits and refunds, money pits, idle resources, this month's forecast and the Advisor tips.

Give it to an AI agent and ask *"where can I save money?"*

## Read a FOCUS export

Organizations on an Enterprise Agreement (EA) or a Microsoft Customer Agreement (MCA) can have Cost Management export their costs to a storage account. aztree can draw its page from those files instead of calling the Cost Management API. Then it needs no Azure login and never waits for throttling. Pay-as-you-go subscriptions can't export these files.

1. In the Azure portal, open **Cost Management → Exports** at the scope you want. Create an export of **Cost and usage details (FOCUS)**: **CSV** with **Gzip**, a daily export of month-to-date costs, with **Overwrite data** on.
2. Use **Export selected dates** to add last month, so there's a period to compare with.
3. Download the export's folder, manifests included:

   ```bash
   az storage blob download-batch --account-name ACCOUNT --source CONTAINER --pattern "DIR/EXPORT/*" --destination ./focus --auth-mode login
   ```

4. Run `aztree --focus ./focus`.

aztree reads the newest run for each day, so repeated runs don't count twice. Download one export at a time: two exports of the same days would overlap. The period ends on the last complete day. When the files cover less than two periods, aztree shortens the period and says so. When a run is missing files, days have no rows, or a day is in two files, the page and the summary say so too, so whoever you send them to sees it.

A file someone sends you works too: `aztree --focus costs.csv`.

Advisor, the idle checks and the forecast need Azure, so they don't run on files. With `ActualCost`, the header shows reservation and savings plan purchases. aztree doesn't read Parquet exports yet.

## Options

| Flag | What it does |
|---|---|
| `--demo` | Fake data, no Azure access needed |
| `--subscription ID_OR_NAME` | Subscription to read; repeat for more. Default: the Azure CLI's current one |
| `--all` | Every enabled subscription you can see, in every tenant you're logged in to |
| `--scope SCOPE` | Any Cost Management scope, e.g. `/providers/Microsoft.Billing/billingAccounts/ID` (untested) |
| `--focus PATH` | Read Cost Management FOCUS export files (CSV or CSV.gz) instead of calling Azure. A file or a folder; repeat for more |
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
- Subscriptions that bill in different currencies are drawn in USD. When Azure can't convert them, the header says the totals mix currencies.
- Some subscriptions have so many resources that a daily breakdown takes more than ten pages of results. For those, the resource view still lists every resource, but with one total per period and no daily chart.
- Marketplace charges keep their publisher's meter names and get no special handling.
- The forecast is Azure's own, for the calendar month, so it doesn't follow `--days`. It's left out when a subscription has none, or when it comes in a different currency from the page.
- Regions show under the portal's names, such as East US. Cost Management also uses short names (US East) and ARM names (eastus) for the same region, and aztree puts them in one box.
- Runs saved by older versions open without the tag view, the forecast and the idle checks. They also show regions under Cost Management's own names, such as "us east".
- Runs save to `~/.aztree/`; set `AZTREE_HOME` to move it. Older versions saved runs to `out/` when you ran them from a clone. To open those, run `aztree --from out/aztree-data.json`.

## Checking against the portal

In the Azure portal, open **Cost Management → Cost analysis**. Choose the cost type you ran with (**Actual cost** by default), daily granularity and the dates in aztree's header.

The totals should match to within a few cents. aztree leaves out the smallest leftovers, under half a cent in each group, and on a big bill they can add up to a few cents. Azure can still add a little to the last day after aztree reads it, so compare soon after a run.

## Privacy

aztree only reads from Azure and never changes anything in your subscriptions. It reads cost data, forecasts, tag names, Advisor recommendations and Resource Graph properties.

Everything it writes goes to `~/.aztree/` (or `AZTREE_HOME`), outside any git repo. Those files hold your subscription IDs and costs, so share the page or the export only with people who should see your bill.

The page loads nothing from the internet.

## Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org). aztree has applied to the SignPath Foundation program; until it signs the first release, the Windows executable stays unsigned.

Only the release workflow signs, and only executables it builds from this repository's tagged commits. Every signing request is approved by hand.

- Committers and reviewers: [Milan Milanović](https://github.com/milanm)
- Approvers: [Milan Milanović](https://github.com/milanm)

This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it. When you run it, aztree calls Azure with your own login to read your costs, and nothing else (see [Privacy](#privacy)).

## Development

```bash
python -m unittest discover -s tests
python -m aztree --demo
```

`packaging/build_native.py` packs aztree into one executable with PyInstaller and checks that it runs. `dotnet/` holds the dotnet tool, a small launcher that runs the executable built for the machine it's on.

The tests need no Azure access. The viewer tests run the page's script in Node and are skipped without it.

## Credits

aztree is a port of [awstree](https://github.com/petricbranko/awstree) by Branko Petric (MIT). It keeps awstree's viewer and its JSON contract and replaces the AWS parts. awstree, in turn, is inspired by [disktree](https://github.com/tobi/disktree) by [Tobi Lütke](https://x.com/tobi).

The region names and several idle checks come from Microsoft's [FinOps toolkit](https://github.com/microsoft/finops-toolkit) (MIT). See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

aztree is not affiliated with or endorsed by Microsoft.

## License

[MIT](LICENSE)
