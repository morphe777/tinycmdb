-- Schéma initial de TinyCMDB sur SQLite.
--
-- Les six tables de l'inventaire, typées, avec de vraies clés étrangères : ce que Baserow
-- appelait un « link_row » est ici une colonne qui pointe vers une ligne, ou une table de
-- liaison quand le lien est multiple. Les noms de colonne sont internes ; le reste de la
-- CMDB parle en noms de champ, voir cmdb/modele.py.
--
-- Une clé naturelle par table, unique : Baserow n'en imposait aucune, et deux lignes pour
-- une même adresse rendaient l'inventaire faux plutôt qu'incomplet.

CREATE TABLE option (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tab     TEXT NOT NULL,
    champ   TEXT NOT NULL,
    valeur  TEXT NOT NULL,
    couleur TEXT NOT NULL DEFAULT 'gray',
    ordre   INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tab, champ, valeur)
);

CREATE TABLE vlan (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    numero     INTEGER,
    subnet     TEXT,
    gateway    TEXT,
    zone       INTEGER REFERENCES option(id) ON DELETE SET NULL,
    dhcp       INTEGER NOT NULL DEFAULT 0,
    dhcp_range TEXT,
    dns        TEXT,
    notes      TEXT
);

CREATE TABLE application (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    status      INTEGER REFERENCES option(id) ON DELETE SET NULL,
    criticality INTEGER REFERENCES option(id) ON DELETE SET NULL,
    url         TEXT,
    url_2       TEXT,
    doc         TEXT,
    notes       TEXT
);

CREATE TABLE node (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    type        INTEGER REFERENCES option(id) ON DELETE SET NULL,
    parent_id   INTEGER REFERENCES node(id) ON DELETE SET NULL,
    vmid        INTEGER,
    status      INTEGER REFERENCES option(id) ON DELETE SET NULL,
    os          TEXT,
    vcpu        INTEGER,
    ram_gb      INTEGER,
    disk_gb     INTEGER,
    criticality INTEGER REFERENCES option(id) ON DELETE SET NULL,
    backup      INTEGER NOT NULL DEFAULT 0,
    guest_agent INTEGER NOT NULL DEFAULT 0,
    source      INTEGER REFERENCES option(id) ON DELETE SET NULL,
    last_seen   TEXT,
    notes       TEXT
);

CREATE TABLE image (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    reference         TEXT NOT NULL UNIQUE,
    registry          TEXT,
    repository        TEXT,
    tag               TEXT,
    digest_local      TEXT,
    available_version TEXT,
    update_available  INTEGER NOT NULL DEFAULT 0,
    cve_critical      INTEGER,
    cve_high          INTEGER,
    last_scan         TEXT,
    last_seen         TEXT,
    notes             TEXT
);

CREATE TABLE container (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uid            TEXT NOT NULL UNIQUE,
    name           TEXT,
    host_id        INTEGER REFERENCES node(id) ON DELETE SET NULL,
    image_id       INTEGER REFERENCES image(id) ON DELETE SET NULL,
    service        TEXT,
    status         INTEGER REFERENCES option(id) ON DELETE SET NULL,
    ports          TEXT,
    restart_policy INTEGER REFERENCES option(id) ON DELETE SET NULL,
    stack          TEXT,
    compose_path   TEXT,
    type           INTEGER REFERENCES option(id) ON DELETE SET NULL,
    last_seen      TEXT,
    notes          TEXT
);

CREATE TABLE ipam (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    address   TEXT NOT NULL UNIQUE,
    ip_int    INTEGER,
    vlan_id   INTEGER REFERENCES vlan(id) ON DELETE SET NULL,
    type      INTEGER REFERENCES option(id) ON DELETE SET NULL,
    node_id   INTEGER REFERENCES node(id) ON DELETE SET NULL,
    fqdn      TEXT,
    hostname  TEXT,
    mac       TEXT,
    vendor    TEXT,
    status    INTEGER REFERENCES option(id) ON DELETE SET NULL,
    source    INTEGER REFERENCES option(id) ON DELETE SET NULL,
    last_seen TEXT,
    notes     TEXT
);

CREATE INDEX container_host ON container(host_id);
CREATE INDEX container_image ON container(image_id);
CREATE INDEX ipam_node ON ipam(node_id);
CREATE INDEX ipam_vlan ON ipam(vlan_id);
CREATE INDEX node_parent ON node(parent_id);

-- Liens multiples vers les applications.
CREATE TABLE node_application (
    node_id        INTEGER NOT NULL REFERENCES node(id) ON DELETE CASCADE,
    application_id INTEGER NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, application_id)
);

CREATE TABLE container_application (
    container_id   INTEGER NOT NULL REFERENCES container(id) ON DELETE CASCADE,
    application_id INTEGER NOT NULL REFERENCES application(id) ON DELETE CASCADE,
    PRIMARY KEY (container_id, application_id)
);

-- Listes à choix multiples (Roles, Capability, et celles des champs personnalisés).
-- Une seule table pour toutes : la ligne visée est désignée par (tab, ligne_id), et le
-- ménage à la suppression d'une ligne est fait par les déclencheurs plus bas.
CREATE TABLE choix (
    tab       TEXT NOT NULL,
    champ     TEXT NOT NULL,
    ligne_id  INTEGER NOT NULL,
    option_id INTEGER NOT NULL REFERENCES option(id) ON DELETE CASCADE,
    PRIMARY KEY (tab, champ, ligne_id, option_id)
);

-- Champs ajoutés depuis l'administration. Ce qui remplace « ajouter un champ dans
-- Baserow » : les champs dont le code dépend restent des colonnes, le reste vit ici.
CREATE TABLE champ_perso (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    tab   TEXT NOT NULL,
    nom   TEXT NOT NULL,
    type  TEXT NOT NULL,
    aide  TEXT,
    ordre INTEGER NOT NULL DEFAULT 0,
    cree  TEXT NOT NULL,
    UNIQUE (tab, nom)
);

CREATE TABLE valeur_perso (
    champ_id INTEGER NOT NULL REFERENCES champ_perso(id) ON DELETE CASCADE,
    ligne_id INTEGER NOT NULL,
    valeur   TEXT,
    PRIMARY KEY (champ_id, ligne_id)
);

-- Ce qui a été fait, par qui. Les écritures de routine du collecteur (Last_seen à chaque
-- passage) n'y figurent pas : elles noieraient le reste. Ses créations et suppressions,
-- si.
CREATE TABLE journal (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    quand    TEXT NOT NULL,
    acteur   TEXT NOT NULL,
    action   TEXT NOT NULL,
    tab      TEXT,
    ligne_id INTEGER,
    libelle  TEXT,
    champ    TEXT,
    avant    TEXT,
    apres    TEXT
);
CREATE INDEX journal_quand ON journal(quand);
CREATE INDEX journal_ligne ON journal(tab, ligne_id);

-- Jetons d'accès à l'API et au serveur MCP. Seule l'empreinte est gardée : le jeton
-- lui-même n'est montré qu'une fois, à sa création.
CREATE TABLE jeton (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nom           TEXT NOT NULL,
    empreinte     TEXT NOT NULL UNIQUE,
    prefixe       TEXT NOT NULL,
    droits        TEXT NOT NULL CHECK (droits IN ('lecture', 'ecriture')),
    cree          TEXT NOT NULL,
    expire        TEXT,
    dernier_usage TEXT,
    revoque       TEXT
);

CREATE TABLE parametre (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);

-- Ménage à la suppression d'une ligne : ses choix multiples et ses champs personnalisés.
CREATE TRIGGER node_menage AFTER DELETE ON node BEGIN
    DELETE FROM choix WHERE tab = 'node' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'node');
END;
CREATE TRIGGER container_menage AFTER DELETE ON container BEGIN
    DELETE FROM choix WHERE tab = 'container' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'container');
END;
CREATE TRIGGER image_menage AFTER DELETE ON image BEGIN
    DELETE FROM choix WHERE tab = 'image' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'image');
END;
CREATE TRIGGER ipam_menage AFTER DELETE ON ipam BEGIN
    DELETE FROM choix WHERE tab = 'ipam' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'ipam');
END;
CREATE TRIGGER vlan_menage AFTER DELETE ON vlan BEGIN
    DELETE FROM choix WHERE tab = 'vlan' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'vlan');
END;
CREATE TRIGGER application_menage AFTER DELETE ON application BEGIN
    DELETE FROM choix WHERE tab = 'application' AND ligne_id = OLD.id;
    DELETE FROM valeur_perso WHERE ligne_id = OLD.id
        AND champ_id IN (SELECT id FROM champ_perso WHERE tab = 'application');
END;

-- Options de départ, pour une installation neuve. Une reprise depuis Baserow les remplace
-- par celles de la base d'origine, couleurs comprises.
INSERT INTO option (tab, champ, valeur, couleur, ordre) VALUES
    ('node', 'Type', 'Physical', 'blue', 1), ('node', 'Type', 'VM', 'green', 2),
    ('node', 'Type', 'LXC', 'orange', 3), ('node', 'Type', 'Appliance', 'red', 4),
    ('node', 'Type', 'Device', 'yellow', 5), ('node', 'Type', 'Camera', 'green', 6),
    ('node', 'Type', 'IoT', 'yellow', 7),
    ('node', 'Status', 'Running', 'green', 1), ('node', 'Status', 'Stopped', 'red', 2),
    ('node', 'Status', 'Retired', 'orange', 3),
    ('node', 'Roles', 'Hypervisor', 'blue', 1), ('node', 'Roles', 'Docker', 'purple', 2),
    ('node', 'Roles', 'Database', 'pink', 3), ('node', 'Roles', 'Network', 'green', 4),
    ('node', 'Roles', 'Storage', 'yellow', 5), ('node', 'Roles', 'Backup', 'green', 6),
    ('node', 'Criticality', 'High', 'red', 1), ('node', 'Criticality', 'Medium', 'yellow', 2),
    ('node', 'Criticality', 'Low', 'green', 3),
    ('node', 'Source', 'Auto', 'blue', 1), ('node', 'Source', 'Manual', 'green', 2),
    ('container', 'Status', 'Running', 'green', 1), ('container', 'Status', 'Exited', 'red', 2),
    ('container', 'Status', 'Restarting', 'orange', 3), ('container', 'Status', 'Paused', 'gray', 4),
    ('container', 'Restart_policy', 'Always', 'green', 1),
    ('container', 'Restart_policy', 'Unless-Stopped', 'blue', 2),
    ('container', 'Restart_policy', 'On-Failure', 'cyan', 3),
    ('container', 'Restart_policy', 'No', 'red', 4),
    ('container', 'Type', 'Docker_Compose', 'purple', 1), ('container', 'Type', 'Container', 'red', 2),
    ('ipam', 'Type', 'Static', 'blue', 1), ('ipam', 'Type', 'DHCP-reservation', 'green', 2),
    ('ipam', 'Type', 'DHCP-pool', 'orange', 3), ('ipam', 'Type', 'VIP', 'red', 4),
    ('ipam', 'Type', 'Gateway', 'gray', 5), ('ipam', 'Type', 'Broadcast', 'cyan', 6),
    ('ipam', 'Status', 'Used', 'blue', 1), ('ipam', 'Status', 'Free', 'green', 2),
    ('ipam', 'Status', 'Reserved', 'orange', 3), ('ipam', 'Status', 'To check', 'red', 4),
    ('ipam', 'Source', 'Auto', 'blue', 1), ('ipam', 'Source', 'Manual', 'green', 2),
    ('vlan', 'zone', 'LAN', 'blue', 1), ('vlan', 'zone', 'DMZ', 'red', 2),
    ('vlan', 'zone', 'SERVER', 'green', 3), ('vlan', 'zone', 'ADMIN', 'gray', 4),
    ('application', 'Status', 'Prod', 'green', 1), ('application', 'Status', 'Test', 'orange', 2),
    ('application', 'Status', 'to Delete', 'red', 3),
    ('application', 'Criticality', 'High', 'red', 1),
    ('application', 'Criticality', 'Medium', 'orange', 2),
    ('application', 'Criticality', 'Low', 'green', 3);

INSERT INTO parametre (cle, valeur) VALUES
    ('sauvegarde_intervalle_heures', '24'),
    ('sauvegarde_conservation', '14'),
    ('journal_conservation_jours', '365');
