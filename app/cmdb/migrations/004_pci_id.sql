-- L'identité matérielle d'une carte PCI : constructeur et modèle de la puce, puis ceux de
-- la carte (`10de:2484 1462:3907`), tels que les rend l'hyperviseur.
--
-- La clé d'un composant PCI est son emplacement (`hôte/pci/0000:01:00`) : c'est ce qui
-- distingue deux cartes identiques dans une même machine. Mais une carte qu'on déplace
-- change d'emplacement, et le collecteur la prenait pour une nouvelle, abandonnant à
-- l'ancienne ligne tout ce qui avait été saisi (nom, référence, mémoire graphique…). Avec
-- cette identité, il reconnaît la carte qui vient de disparaître ailleurs et reprend sa
-- ligne (voir `collector/main.py:run_component_pass`).

ALTER TABLE composant ADD COLUMN pci_id TEXT;
