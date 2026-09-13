# Image de TinyCMDB : deux services, un seul artefact. Le collecteur et la console web
# partagent le même code et les mêmes dépendances ; seule la commande les distingue
# (voir compose.yaml). Construire deux images pour ça coûterait deux fois le temps de
# construction et créerait la possibilité qu'elles divergent.

# Binaire Cup (5 Mo, sources/cup.py l'appelle en sous-processus) : copié depuis l'image
# officielle plutôt qu'un serveur séparé, jamais d'accès Docker (-s none), voir cup.py.
FROM ghcr.io/sergi0g/cup:v3.5.1 AS cup

# Binaire Trivy (sources/trivy.py) : scanne par référence/digest, aucun accès Docker non
# plus. Sa base de vulnérabilités (~1 Go) vit dans /cache, un volume persistant (voir
# compose.yaml) — sans ça, retéléchargement complet à chaque redémarrage du conteneur.
FROM aquasec/trivy:0.74.0 AS trivy

# Les deux sont épinglés sur une version et non sur `latest` : ce sont des outils dont le
# comportement se lit dans l'inventaire. Deux constructions à quinze jours d'écart doivent
# donner le même résultat, sinon un changement de CVE ne veut plus rien dire.

FROM python:3.14-slim

# iputils-ping : vérification de vie best-effort pour IPAM.status (voir netcheck.py). Le
# paquet Debian pose une capability CAP_NET_RAW sur le binaire, utilisable sans root.
RUN apt-get update && apt-get install -y --no-install-recommends iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=cup /cup /usr/local/bin/cup
COPY --from=trivy /usr/local/bin/trivy /usr/local/bin/trivy
RUN chmod +x /usr/local/bin/cup /usr/local/bin/trivy

# UID fixe : le volume ./trivy-cache (bind-mount, voir compose.yaml) doit appartenir à cet
# utilisateur côté hôte pour que Trivy puisse y écrire sa base de vulnérabilités.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin tinycmdb

WORKDIR /app

# Les dépendances avant le code : elles changent rarement, le code souvent. Inverser les
# deux relancerait un `pip install` complet à chaque modification d'un gabarit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Le code est dans l'image. Pour développer sans reconstruire, remonter ./app par-dessus :
# voir compose.override.yaml.example.
COPY app/ /app/

# Le cache Trivy est monté en volume. Le créer ici, appartenant à l'utilisateur du
# conteneur, est ce qui rend un volume nommé utilisable : Docker initialise un volume vide
# à partir du répertoire correspondant de l'image, propriétaire et permissions compris.
# Sans ce répertoire, le volume est créé root, Trivy ne peut rien y écrire, et le message
# d'erreur ne désigne pas la cause — panne classique des déploiements par Portainer.
RUN mkdir -p /cache && chown tinycmdb:tinycmdb /cache

USER tinycmdb

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "collector.main"]
