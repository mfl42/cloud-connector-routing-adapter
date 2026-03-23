# Features

Ce document liste en langage simple toutes les fonctions de l'adaptateur
cloud-connector-routing-adapter. Il est destiné aux utilisateurs et
contributeurs qui veulent comprendre ce que fait le logiciel sans lire le code.

---

## Contexte : Sylva, Kubernetes et les CRDs

### Qu'est-ce que Sylva ?

Sylva est un framework open source de la Linux Foundation, porte par des
operateurs telecom (Deutsche Telekom, Orange, etc.). Son objectif : fournir
une pile logicielle complete pour deployer et gerer des infrastructures
telecom sur Kubernetes.

Dans Sylva, le reseau n'est pas configure manuellement sur chaque routeur.
A la place, un operateur declare la configuration souhaitee dans Kubernetes,
et des composants logiciels (les "connecteurs") se chargent de l'appliquer
sur les equipements reels.

### Qu'est-ce qu'un CRD ?

Un CRD (Custom Resource Definition) est un mecanisme Kubernetes qui permet
de definir ses propres types d'objets. Par defaut, Kubernetes gere des Pods,
des Services, des Deployments. Un CRD permet d'ajouter un nouveau type —
par exemple `NodeNetworkConfig` — que Kubernetes stocke, versionne et
surveille exactement comme ses objets natifs.

Un CRD permet :
- de decrire une configuration souhaitee de maniere declarative (YAML/JSON)
- de la stocker dans l'API Kubernetes avec versioning et controle d'acces
- de declencher des actions automatiques quand l'objet est cree, modifie ou
  supprime (via un controller ou un operateur)
- de rapporter un statut (`status` subresource) pour indiquer si la
  configuration a ete appliquee avec succes

### Les APIs network-connector de Sylva

Le composant `network-connector` de Sylva definit deux CRDs principaux pour
la configuration reseau :

**NodeNetworkConfig** — configuration reseau L3 d'un noeud :
- VRFs (Virtual Routing and Forwarding) avec tables de routage
- routes statiques IPv4/IPv6
- voisins BGP avec ASN, timers, filtres import/export
- policy routes (routage par politique avec filtrage par prefixe/port/protocole)
- interfaces rattachees aux VRFs (Ethernet, VLAN, bonding, etc.)
- domaines Layer2 avec VXLAN/EVPN (VNI, bridge, IRB)
- fabric VRFs avec route targets et filtres EVPN

**NodeNetplanConfig** — configuration reseau L2/L3 d'un noeud au format netplan :
- adresses IP des interfaces
- routes statiques avec metrique
- DHCP v4/v6
- MTU
- serveurs DNS

Ces CRDs sont la "source de verite" : ils decrivent l'etat reseau desire.
Un operateur humain ou un systeme d'orchestration les cree/modifie dans
Kubernetes. Un connecteur (comme cet adaptateur) les lit et les applique
sur l'equipement cible.

### Role de cet adaptateur

Cet adaptateur est le connecteur entre les CRDs Sylva et un routeur VyOS.
Il lit les objets `NodeNetworkConfig` et `NodeNetplanConfig` depuis
Kubernetes, les traduit en commandes VyOS (`set ...`), et les envoie au
routeur via son API HTTPS.

Il fonctionne en boucle : il surveille les changements Kubernetes, calcule
les differences avec la derniere configuration appliquee, et envoie
uniquement ce qui a change au routeur.

---

## Traduction reseau

### VRF (Virtual Routing and Forwarding)

Chaque VRF declare dans Kubernetes produit :
- une table de routage VyOS (`set vrf name '<nom>' table '<id>'`)
- le rattachement des interfaces au VRF
- la prise en charge des sous-interfaces VLAN (`eth0.100` traduit en `vif 100`)

Trois sources de VRF sont reconnues : `clusterVRF`, `fabricVRFs`, `localVRFs`.

### Routes statiques

Chaque route statique produit une commande VyOS avec :
- le prefixe de destination (IPv4 ou IPv6, detecte automatiquement)
- l'adresse du prochain saut ou l'interface de sortie
- la famille d'adresse est detectee automatiquement (route vs route6)

### Policy routes (routage par politique)

Les regles de filtrage du trafic sont traduites en `policy route` VyOS :
- filtrage par prefixe source/destination, protocole (tcp, udp, icmp, gre),
  ports source/destination
- une regle VyOS par protocole supporte (rule_id incremente)
- action : `set nexthop` (prioritaire), `set vrf`, ou `set table`
- detection automatique IPv4 vs IPv6 pour le choix `policy route` vs `policy route6`

### BGP

Configuration complete des voisins BGP par VRF :
- ASN local, router-id
- adresse du voisin, ASN distant
- update-source, ebgp-multihop, password
- timers keepalive/holdtime (les deux doivent etre presents)
- BFD, graceful-restart
- familles d'adresses : ipv4-unicast, ipv6-unicast, l2vpn-evpn

### Filtres BGP (import/export)

Les objets `importFilter` et `exportFilter` du CRD sont compiles en objets
VyOS :
- **route-map** : une regle par item de filtre, avec action permit/deny
- **prefix-list** (IPv4) ou **prefix-list6** (IPv6) : pour le filtrage par
  prefixe, avec contraintes de longueur ge/le
- **community-list** : pour le filtrage par communaute BGP, avec exact-match
- modifications de route : ajout/suppression de communautes, mode additif
- regle par defaut (rule 65535) pour l'action par defaut du filtre
- liaison au voisin BGP via `route-map import|export`

Les references simples par nom (`routeMap`, `prefixList`, `distributionList`)
ne sont pas compilees et restent marquees comme non supportees.

### Interfaces (NodeNetplanConfig)

Traduction des interfaces reseau depuis le format netplan :
- adresses IP (CIDR)
- DHCP v4 et v6
- MTU
- routes statiques avec metrique optionnelle
- serveurs DNS
- deux formats supportes : legacy (`spec.interfaces`) et natif netplan
  (`spec.desiredState.network.ethernets`, bonds, bridges, vlans, etc.)
- detection automatique du type d'interface (ethernet, bonding, bridge,
  dummy, wireguard, vxlan, etc.)

### VXLAN et EVPN (layer2)

Chaque domaine Layer2 (`spec.layer2s`) produit :
- une interface VXLAN (`set interfaces vxlan vxlan<VNI> vni '<VNI>'`)
- un pont bridge (`set interfaces bridge br<VLAN>`)
- le rattachement du VXLAN au bridge
- si IRB present : adresses IP, adresse MAC, rattachement VRF sur le bridge

Pour les fabric VRFs avec EVPN :
- liaison VNI au VRF (`set vrf name '<vrf>' vni '<vni>'`)
- activation de la famille d'adresses l2vpn-evpn
- `advertise-all-vni` emis implicitement
- route targets export/import
- filtre EVPN export compile en route-map
- imports inter-VRF avec filtre optionnel

Limitations connues :
- l'adresse source VTEP n'est pas dans le CRD (VyOS utilise le loopback)
- le Route Distinguisher est auto-genere par VyOS
- les mirrorAcls (mirroring GRE) ne sont pas encore traduits

---

## Reconciliation et resilience

### Detection de changement

L'adaptateur calcule un digest SHA-256 de la liste de commandes. Si le
digest n'a pas change depuis la derniere application, aucune commande n'est
envoyee (noop). Cela garantit l'idempotence.

### Application par lot (batch)

Toutes les commandes sont envoyees en une seule requete HTTP au routeur
VyOS (`/configure-list`). Pas de requete par commande.

### Diff partiel

Quand un document est modifie (ex: suppression d'un peer BGP), l'adaptateur
calcule les commandes qui ont disparu et genere des `delete` avant d'envoyer
les nouveaux `set`. Les voisins BGP supprimes sont consolides en un seul
`delete ... neighbor` au lieu de supprimer chaque feuille individuellement.

### Teardown complet

Quand un document est supprime de Kubernetes, toutes les commandes
correspondantes sont inversees en `delete` et envoyees au routeur.

### Rollback en cas d'echec

Si l'envoi des commandes echoue (commit error VyOS), l'adaptateur appelle
`discard` pour annuler les changements en attente. Le prochain cycle repart
de zero.

### Tolerance aux erreurs idempotentes

Si le routeur repond "already exists" lors d'un fallback sequentiel, c'est
considere comme un succes (la commande est deja appliquee).

---

## Observation et statut

### Conditions Kubernetes

Chaque document recoit des conditions de statut patchees sur le subresource
Kubernetes :

| Condition | Signification |
|-----------|---------------|
| DesiredSeen | L'adaptateur a enregistre la configuration desiree |
| Applied | Une configuration a ete appliquee au routeur |
| InSync | La configuration desiree et appliquee sont identiques |
| Reconciling | Un changement est en attente d'application |
| Degraded | La derniere operation a echoue |
| Available | L'adaptateur est en phase avec l'etat desire |
| HasWarnings | Des avertissements de traduction existent |
| HasUnsupported | Des elements non supportes ont ete detectes |
| Deleted | Le document a ete supprime de la source |
| Error | Erreur lors de la derniere operation |

### Rapport de statut local

Un fichier JSON de statut est genere localement avec le detail par document :
phase, revision, digest, compteurs de commandes/warnings/unsupported,
timestamps.

---

## Infrastructure du controller

### Watch Kubernetes (informer)

Un thread en arriere-plan surveille en continu les changements Kubernetes.
Les evenements sont pousses dans une file d'attente. La boucle principale
se reveille immediatement quand un changement arrive, au lieu d'attendre
la fin d'un timeout.

- backoff exponentiel en cas d'erreurs (0.2s a 30s)
- resynchronisation complete toutes les 30 minutes
- relist automatique sur erreur 410 (watch expire)

### Election de leader

Si plusieurs instances de l'adaptateur tournent en parallele, un mecanisme
de Lease Kubernetes (`coordination.k8s.io/v1`) garantit qu'une seule
instance envoie des commandes au routeur.

- les non-leaders continuent de calculer les differences localement
- si le leader tombe, un autre prend le relais automatiquement
- configurable via `--enable-leader-election`, `--leader-id`,
  `--lease-namespace`, `--lease-duration-seconds`

### Mode cluster-scoped

L'adaptateur peut surveiller les CRDs au niveau cluster (tous les namespaces)
au lieu d'un seul namespace. Configurable via `--cluster-scoped-source` et
`--cluster-scoped-status`.

---

## Tests

Trois suites de tests locaux valident chaque feature sans cluster ni routeur :

### Boundary (17 scenarios)

Tests deterministes de cas limites :
- interfaces (types inconnus, VLAN, loopback)
- routes statiques et policy routes (prefixes invalides, ports extremes)
- BGP (familles inconnues, peer sans adresse, timers incomplets)
- netplan (routes sans via, adresses invalides, nameservers)
- valeurs invalides (prefixes/IPs malformes)
- reconciliation sans commande (document vide)
- structures malformees (spec pas un objet, routes pas une liste)
- format desiredState (wrapped, unwrapped, bonds, vide)
- regex BGP avec quotes echappees
- grande topologie (10 VRF x 10 peers x 100 routes x 2 VLAN = 1240 commandes)
- URLs cluster-scoped vs namespace-scoped
- conditions CRA (5 etats de phase)
- compilation route-map BGP (prefix, community, exact-match, modifications)
- election de leader (expiration, parsing, NoopLeaseManager)
- edge cases filtres BGP (action "next", matchers vides, ge>le, conflits)
- EVPN + VXLAN + layer2 + IRB (fabric VRF complet)
- auto-decouverte API (variants, tolerance 404, URLs t-caas vs sylva.io)

### Chaos (13 scenarios)

Injection de fautes simulant des pannes reelles :
- timeout VyOS puis recovery
- echec status Kubernetes 409 puis convergence
- churn de watch, suppression, prune des tombstones
- retry HTTP patch Kubernetes (transport loss, 503)
- echec commit VyOS puis rollback + retry
- URL patch status en mode cluster-scoped
- propagation du flag cluster_scoped_status
- echec route-map apply puis retry
- non-leader skip apply, leader apply
- event queue informer (2 iterations, changement detecte)
- exception pendant acquisition du lease
- renouvellement lease multi-cycle (leader, leader, non-leader)
- documents multi-API-group (sylva.io + t-caas dans un seul batch)

### Fuzz (120 iterations, ~6300 commandes)

Generation aleatoire de configurations incluant :
- VRFs avec noms, tables, routes, peers BGP aleatoires
- filtres BGP structures (prefix/community matchers, ge/le, modifications)
- champs EVPN sur fabric VRFs (vni, route targets, export filters)
- layer2s complets (vni, vlan, mtu, routeTarget, IRB)
- policy routes avec protocoles mixtes
- netplan avec interfaces, adresses, routes, DNS
- verification : aucun crash, deuxieme passe = 0 commandes en attente

---

## Compatibilite API upstream

L'adaptateur est compatible avec plusieurs versions de l'API upstream Sylva
sans configuration :

- `network.t-caas.telekom.com/v1alpha1` — version production T-CAAS
- `network.t-caas.telekom.com/v1beta1` — alias forward-compatible
- `sylva.io/v1alpha1` — projet upstream Sylva (Linux Foundation)

Au demarrage en mode `--source kubernetes`, l'adaptateur enregistre tous les
variants connus et tente de lister les documents de chacun. Les API groups
qui n'existent pas sur le cluster (reponse 404) sont ignores silencieusement.
Aucun flag de configuration n'est necessaire.

Limitations :
- la liste des variants est codee en dur (pas de decouverte Kubernetes `/apis`)
- un nouveau groupe API upstream necessite l'ajout d'une ligne dans le code
- les CRDs locaux (`k8s/crds/`) sont fixes sur `network.t-caas.telekom.com`

---

## Limitations connues

### Traduction

- **mirrorAcls** — le mirroring de trafic par encapsulation GRE (champ
  `layer2s.<name>.mirrorAcls`) est detecte mais pas traduit. Le support
  depend de la version VyOS et n'est pas encore mappe.
- **References route-map par nom** — les champs `routeMap`, `prefixList`,
  `distributionList` sur les peers BGP sont reconnus mais pas compiles en
  objets VyOS. Seuls les filtres structures `importFilter`/`exportFilter`
  sont compiles. Les references par nom restent marquees non supportees.
- **VTEP source-address** — l'IP source du tunnel VXLAN n'est pas dans le
  CRD upstream. VyOS utilise le loopback ou la route par defaut. Pour un
  controle explicite en fabric EVPN multi-noeud, un parametre CLI
  `--vtep-source-address` pourrait etre ajoute.
- **Route Distinguisher** — absent du CRD. VyOS auto-genere le RD. Pas de
  surcharge possible via l'adaptateur.
- **Readiness par interface** — les conditions de statut CRA rapportent la
  sante au niveau document (Available, Reconciling, Degraded) mais pas par
  interface. Correler la config desiree avec l'etat operationnel VyOS
  necessite de lire l'API VyOS, qui n'a pas de contrat stable upstream.
- **Validation schema CRD** — l'adaptateur ne valide pas le schema des CRDs.
  Il parse ce qu'il comprend et marque le reste comme warning ou unsupported.

### Prochaines etapes possibles

1. **Tests live sur le lab VyOS** — valider les commandes generees sur le
   routeur reel VyOS du lab, notamment EVPN/VXLAN
2. **CI/CD GitHub Actions** — automatiser les 3 suites de tests sur chaque PR
3. **Suivi API upstream Sylva** — surveiller le renommage du groupe API lors
   de la fusion dans sylva-core
4. **Packaging container + Helm** — image Docker et chart Helm pour deploiement
   Kubernetes (prerequis pour l'election de leader en production)
5. **Metriques Prometheus** — compteurs de commandes, erreurs, latence reconcile
