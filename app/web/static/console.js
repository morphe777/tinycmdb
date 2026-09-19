/* Comportements de la console.
 *
 * Servi en fichier séparé et non plus au bas de chaque page : ce code est identique d'une
 * page à l'autre, le navigateur le garde en cache, et le paramètre `?v=` de l'horodatage
 * le renouvelle à chaque modification — le même mécanisme que pour la feuille de style,
 * indispensable ici puisqu'on modifie un fichier monté en lecture seule et qu'on redémarre.
 *
 * Reste dans l'en-tête de `base.html`, et doit y rester : le choix de thème, qui doit
 * s'appliquer avant le premier rendu, et la cascade d'icônes, appelée par l'attribut
 * `onerror` d'images qui peuvent échouer avant l'exécution de ce fichier.
 *
 * Tout ce qui suit est une amélioration : chaque écran fonctionne sans. */

// ---------------------------------------------------------------- menu principal
(function () {
  var bouton = document.querySelector(".menu-bascule");
  var nav = document.getElementById("menu-principal");
  if (!bouton || !nav) { return; }
  bouton.addEventListener("click", function () {
    var ouvert = nav.classList.toggle("ouvert");
    bouton.setAttribute("aria-expanded", String(ouvert));
  });
  nav.addEventListener("click", function (e) {
    if (e.target.tagName === "A") { nav.classList.remove("ouvert"); }
  });
})();

// ---------------------------------------------------------------- sélecteur de thème
(function () {
  var racine = document.documentElement;
  function courant() {
    try { return localStorage.getItem("theme") || "auto"; } catch (e) { return "auto"; }
  }
  function appliquer(valeur) {
    if (valeur === "auto") { delete racine.dataset.theme; }
    else { racine.dataset.theme = valeur === "sombre" ? "dark" : "light"; }
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.themeSet === valeur));
    });
  }
  document.querySelectorAll("[data-theme-set]").forEach(function (bouton) {
    bouton.addEventListener("click", function () {
      var valeur = bouton.dataset.themeSet;
      try { localStorage.setItem("theme", valeur); } catch (e) { /* sans persistance */ }
      appliquer(valeur);
    });
  });
  appliquer(courant());
})();

// ---------------------------------------------------------------- bulles d'aide
// Une seule ouverte à la fois, fermeture au clic extérieur ou à Échap.
(function () {
  document.addEventListener("click", function (evenement) {
    document.querySelectorAll("details.aide[open]").forEach(function (aide) {
      if (!aide.contains(evenement.target)) { aide.open = false; }
    });
  });
  document.addEventListener("keydown", function (evenement) {
    if (evenement.key === "Escape") {
      document.querySelectorAll("details.aide[open]").forEach(function (a) { a.open = false; });
    }
  });
})();

// ---------------------------------------------------------------- sélecteurs filtrables
// Le gabarit écrit une liste de cases à cocher, panneau déplié et bouton masqué : sans ce
// script, c'est cette liste qui s'affiche et le formulaire fonctionne. Le script inverse
// les deux, et n'ajoute qu'un filtre et un résumé de la sélection.
(function () {
  var combos = document.querySelectorAll("[data-combo]");
  if (!combos.length) { return; }

  // Comparaison sans accent ni casse : « securite » doit trouver « Sécurité ».
  function pliage(texte) {
    return texte.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  }

  combos.forEach(function (combo) {
    var declencheur = combo.querySelector(".combo-declencheur");
    var panneau = combo.querySelector(".combo-panneau");
    var filtre = combo.querySelector(".combo-filtre");
    var resume = combo.querySelector(".combo-resume");
    var rien = combo.querySelector(".combo-rien");
    var etiquettes = Array.prototype.slice.call(combo.querySelectorAll("label[data-libelle]"));
    if (!declencheur || !panneau || !resume) { return; }

    declencheur.hidden = false;
    if (filtre) { filtre.hidden = false; }
    panneau.hidden = true;
    combo.classList.add("combo-actif");

    function majResume() {
      var choisis = etiquettes.filter(function (l) {
        return l.querySelector("input").checked;
      }).map(function (l) { return l.dataset.libelle; });
      // Au-delà de deux, on compte au lieu d'énumérer. Six noms mis bout à bout font une
      // ligne qu'on ne lit pas, et qui pousse la largeur du bouton bien au-delà de la
      // cellule qui le contient — une troncature visuelle n'y suffit pas, la largeur est
      // déjà réservée. L'infobulle garde la liste entière à portée de survol.
      if (choisis.length === 0) {
        resume.textContent = resume.dataset.vide;
      } else if (choisis.length <= 2) {
        resume.textContent = choisis.join(", ");
      } else {
        resume.textContent = choisis.length + " " + (resume.dataset.unite || "éléments");
      }
      resume.classList.toggle("combo-vide", choisis.length === 0);
      declencheur.title = choisis.length > 2 ? choisis.join(", ") : "";
    }

    function appliquerFiltre() {
      var q = pliage(filtre ? filtre.value.trim() : "");
      var visibles = 0;
      etiquettes.forEach(function (l) {
        var ok = !q || pliage(l.dataset.libelle).indexOf(q) !== -1;
        l.hidden = !ok;
        if (ok) { visibles += 1; }
      });
      if (rien) { rien.hidden = visibles > 0; }
    }

    // Le panneau est positionné par rapport à la fenêtre : c'est ce qui lui permet de
    // déborder d'une boîte de dialogue qui défile, au lieu d'y être découpé. En
    // contrepartie, ses coordonnées doivent être calculées à chaque ouverture — et
    // suivies tant qu'il est ouvert.
    function positionner() {
      var cadre = declencheur.getBoundingClientRect();
      var largeur = Math.min(Math.max(cadre.width, 340), window.innerWidth - 24);
      panneau.style.width = largeur + "px";
      panneau.style.left = Math.max(12, Math.min(cadre.left, window.innerWidth - largeur - 12)) + "px";

      // Basculé au-dessus du champ lorsque le bas de la fenêtre est trop proche, plutôt
      // que de déborder hors de l'écran.
      var hauteur = panneau.offsetHeight;
      var place = window.innerHeight - cadre.bottom - 12;
      if (place < hauteur && cadre.top > place) {
        panneau.style.top = Math.max(12, cadre.top - hauteur - 4) + "px";
      } else {
        panneau.style.top = (cadre.bottom + 4) + "px";
      }
    }

    function ouvrir(oui) {
      panneau.hidden = !oui;
      declencheur.setAttribute("aria-expanded", String(oui));
      if (!oui) { return; }
      if (filtre) { filtre.value = ""; appliquerFiltre(); }
      positionner();
      if (filtre) { filtre.focus(); }
    }

    combo.repositionner = function () { if (!panneau.hidden) { positionner(); } };

    declencheur.addEventListener("click", function () { ouvrir(panneau.hidden); });
    combo.addEventListener("change", majResume);
    if (filtre) {
      filtre.addEventListener("input", appliquerFiltre);
      filtre.addEventListener("keydown", function (e) {
        // Entrée dans un champ de filtre soumettrait le formulaire : ce n'est pas là
        // qu'on valide une fiche.
        if (e.key === "Enter") { e.preventDefault(); }
      });
    }
    combo.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !panneau.hidden) {
        e.stopPropagation();
        ouvrir(false);
        declencheur.focus();
      }
    });
    majResume();
  });

  // Le panneau étant ancré à la fenêtre, il ne suit pas la page : on le replace. Capture
  // activée pour recevoir aussi les défilements internes (la boîte de dialogue).
  function replacerTout() {
    document.querySelectorAll(".combo-actif").forEach(function (combo) {
      if (combo.repositionner) { combo.repositionner(); }
    });
  }
  window.addEventListener("scroll", replacerTout, true);
  window.addEventListener("resize", replacerTout);

  document.addEventListener("click", function (e) {
    document.querySelectorAll(".combo-actif").forEach(function (combo) {
      if (combo.contains(e.target)) { return; }
      var panneau = combo.querySelector(".combo-panneau");
      if (panneau && !panneau.hidden) {
        panneau.hidden = true;
        combo.querySelector(".combo-declencheur").setAttribute("aria-expanded", "false");
      }
    });
  });
})();

// ---------------------------------------------------------------- fenêtres de création
// Les déclencheurs sont de vrais liens vers la même page, paramètre `nouvelle` en plus :
// sans script, le serveur renvoie la page avec le formulaire ouvert dans le flux. Avec,
// on évite l'aller-retour et on présente la même fenêtre en modale.
(function () {
  function modaliser(dialogue) {
    if (typeof dialogue.showModal !== "function") { return false; }
    if (dialogue.open) { dialogue.close(); }
    dialogue.showModal();
    return true;
  }

  // Formulaire déjà ouvert par le serveur (lien suivi sans script, ou création refusée
  // qu'on réaffiche remplie) : on le passe en modale pour une présentation identique.
  document.querySelectorAll("dialog.dialogue[open]").forEach(modaliser);

  document.addEventListener("click", function (e) {
    var lien = e.target.closest && e.target.closest("[data-nouvelle]");
    if (lien) {
      var cible = (lien.getAttribute("href") || "").split("#")[1];
      var dialogue = cible && document.getElementById(cible);
      if (!dialogue) { return; }            // repli : le lien suit son cours
      var adresse = lien.dataset.nouvelle;
      var champ = dialogue.querySelector('input[name="champ.Address"]');
      if (champ) { champ.value = adresse || ""; }
      if (!modaliser(dialogue)) { return; }
      e.preventDefault();
      var premier = dialogue.querySelector("input[type=text], textarea, select");
      if (premier) { premier.focus(); }
      return;
    }

    var fermeture = e.target.closest && e.target.closest("[data-fermer]");
    if (fermeture) {
      var boite = fermeture.closest("dialog");
      if (!boite) { return; }
      e.preventDefault();
      boite.close();
      // L'adresse de la page porte encore le paramètre qui a ouvert le formulaire :
      // la nettoyer évite qu'un rechargement le rouvre.
      if (window.history && history.replaceState && location.search.indexOf("nouvelle=") !== -1) {
        history.replaceState(null, "", location.pathname);
      }
    }
  });
})();
