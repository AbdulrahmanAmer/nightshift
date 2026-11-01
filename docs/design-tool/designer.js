/* NightShift UI sketchpad. Vanilla JS, no dependencies, works offline (http or file://). */
(() => {
  'use strict';

  /* ================================================================== constants */
  const GRID = 8;
  const MAX_HISTORY = 100;
  const STORAGE_KEY = 'nightshift-design-tool/project';
  const UI_KEY = 'nightshift-design-tool/ui';
  const ZOOM_MIN = 0.5;
  const ZOOM_MAX = 2;
  const ZOOM_STEPS = [0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
  const PAGE_SIZES = [
    { w: 1920, h: 1080, label: '1920 × 1080 · Desktop HD' },
    { w: 1366, h: 768, label: '1366 × 768 · Laptop' },
    { w: 1024, h: 768, label: '1024 × 768 · Tablet' },
    { w: 390, h: 844, label: '390 × 844 · Phone' }
  ];
  const DEFAULT_PAGE_NAMES = ['Queue', 'Create', 'Library', 'Assistant', 'Channels', 'Results', 'Settings'];
  const DIRS = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];

  /* BEGIN EMBEDDED LIBRARY (copy of styles-library.json, used when the file cannot be fetched, e.g. file://) */
  const EMBEDDED_LIBRARY = {"format":"nightshift-style-library","version":1,"notes":"Each set has tokens (CSS values) and component render rules. In a rule, $name refers to a token and $sp0..$sp6 to the spacing scale. Edit freely; the tool reads this file on start (when served over http).","tokens":["background","pageTint","surface","surfaceAlt","text","muted","accent","accentText","accentSoft","border","track","mediaBg","ok","warn","danger","info","radius","radiusSm","radiusXs","borderWidth","shadow","shadowSm","backdrop","font","spacing"],"sets":[{"id":"bento-light","name":"Bento light","scheme":"light","description":"White cards on white or powder-blue, generous 24 px radii, soft shadow.","tokens":{"background":"#ffffff","pageTint":"#e7eff9","surface":"#ffffff","surfaceAlt":"#f2f5f9","text":"#1c2433","muted":"#6a7385","accent":"#3a6fe3","accentText":"#ffffff","accentSoft":"#e5edfc","border":"#e3e8f0","track":"#dde3ec","mediaBg":"#1f2633","ok":"#1f9d6b","warn":"#c98a12","danger":"#d94848","info":"#3a6fe3","radius":"24px","radiusSm":"12px","radiusXs":"9px","borderWidth":"0px","shadow":"0 1px 2px rgba(22,34,58,.04), 0 10px 28px rgba(22,34,58,.08)","shadowSm":"0 1px 2px rgba(22,34,58,.08)","backdrop":"none","font":"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"$surfaceAlt","color":"$text","border":"none","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"none"},"button.ghost":{"background":"transparent","color":"$accent","border":"none","borderRadius":"$radiusSm","fontWeight":"600"},"chip":{"background":"$surfaceAlt","color":"$text","border":"1px solid transparent","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1px solid transparent"},"tabs":{"background":"$surfaceAlt","borderRadius":"14px","padding":"4px","border":"none"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"$surface","color":"$text","boxShadow":"$shadowSm","fontWeight":"600"},"toggle.track":{"background":"$track","borderRadius":"999px"},"toggle.trackOn":{"background":"$accent"},"toggle.knob":{"background":"#ffffff","borderRadius":"999px","boxShadow":"0 1px 2px rgba(0,0,0,.25)"},"input":{"background":"$surfaceAlt","color":"$muted","border":"1px solid transparent","borderRadius":"$radiusSm"},"text.heading":{"color":"$text","fontSize":"24px","fontWeight":"700","letterSpacing":"-0.01em"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"#ffffff","borderRadius":"$radius","boxShadow":"$shadow"},"listrow":{"background":"transparent","color":"$text","borderBottom":"1px solid $border","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600"},"pill":{"borderRadius":"999px","fontWeight":"600","fontSize":"12px"},"progress.track":{"background":"$track","borderRadius":"999px"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"1px dashed $border"},"divider":{"background":"$border"},"icon":{"color":"$text"}}},{"id":"night-studio","name":"Night studio","scheme":"dark","description":"Dark slate surfaces with a warm amber accent.","tokens":{"background":"#171b21","pageTint":"#1d222a","surface":"#222831","surfaceAlt":"#2b323c","text":"#ece8e1","muted":"#98a1ad","accent":"#f0a73a","accentText":"#1f1505","accentSoft":"rgba(240,167,58,.16)","border":"#333b46","track":"#3a424e","mediaBg":"#0d1015","ok":"#4cc38a","warn":"#f0a73a","danger":"#f06a6a","info":"#6ea8fe","radius":"14px","radiusSm":"8px","radiusXs":"6px","borderWidth":"1px","shadow":"0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.35)","shadowSm":"0 1px 2px rgba(0,0,0,.4)","backdrop":"none","font":"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"$surface","color":"$text","border":"1px solid $border","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"$shadowSm"},"button.ghost":{"background":"transparent","color":"$accent","border":"none","borderRadius":"$radiusSm","fontWeight":"600"},"chip":{"background":"$surfaceAlt","color":"$text","border":"1px solid transparent","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1px solid transparent"},"tabs":{"background":"$surfaceAlt","borderRadius":"$radiusSm","padding":"4px","border":"none"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"#3a424e","color":"$text","boxShadow":"$shadowSm","fontWeight":"600"},"toggle.track":{"background":"$track","borderRadius":"999px"},"toggle.trackOn":{"background":"$accent"},"toggle.knob":{"background":"#f4f1ec","borderRadius":"999px","boxShadow":"0 1px 2px rgba(0,0,0,.25)"},"input":{"background":"$surface","color":"$muted","border":"1px solid $border","borderRadius":"$radiusSm"},"text.heading":{"color":"$text","fontSize":"24px","fontWeight":"700","letterSpacing":"-0.01em"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"#ffffff","borderRadius":"$radius","boxShadow":"$shadow"},"listrow":{"background":"transparent","color":"$text","borderBottom":"1px solid $border","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600"},"pill":{"borderRadius":"999px","fontWeight":"600","fontSize":"12px"},"progress.track":{"background":"$track","borderRadius":"999px"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"1px dashed $border"},"divider":{"background":"$border"},"icon":{"color":"$text"}}},{"id":"flat","name":"Flat","scheme":"light","description":"Solid fills, no shadows, no borders, small radii.","tokens":{"background":"#ffffff","pageTint":"#f4f6f8","surface":"#f1f3f5","surfaceAlt":"#e6e9ed","text":"#1e2328","muted":"#66707a","accent":"#2d7ff0","accentText":"#ffffff","accentSoft":"#dbe9fd","border":"#dde1e6","track":"#d3d8de","mediaBg":"#2a3037","ok":"#22a06b","warn":"#d4900f","danger":"#e04848","info":"#2d7ff0","radius":"8px","radiusSm":"6px","radiusXs":"4px","borderWidth":"0px","shadow":"none","shadowSm":"none","backdrop":"none","font":"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"$surfaceAlt","color":"$text","border":"none","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"$shadowSm"},"button.ghost":{"background":"transparent","color":"$accent","border":"none","borderRadius":"$radiusSm","fontWeight":"600"},"chip":{"background":"$surfaceAlt","color":"$text","border":"1px solid transparent","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1px solid transparent"},"tabs":{"background":"$surface","borderRadius":"$radiusSm","padding":"4px","border":"none"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"$accent","color":"$accentText","boxShadow":"$shadowSm","fontWeight":"600"},"toggle.track":{"background":"$track","borderRadius":"999px"},"toggle.trackOn":{"background":"$accent"},"toggle.knob":{"background":"#ffffff","borderRadius":"999px","boxShadow":"0 1px 2px rgba(0,0,0,.25)"},"input":{"background":"$surface","color":"$muted","border":"none","borderRadius":"$radiusSm"},"text.heading":{"color":"$text","fontSize":"24px","fontWeight":"700","letterSpacing":"-0.01em"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"#ffffff","borderRadius":"$radius","boxShadow":"$shadow"},"listrow":{"background":"$surfaceAlt","color":"$text","borderBottom":"2px solid $surface","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600"},"pill":{"borderRadius":"999px","fontWeight":"600","fontSize":"12px"},"progress.track":{"background":"$track","borderRadius":"999px"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"none"},"divider":{"background":"$border"},"icon":{"color":"$text"}}},{"id":"outlined","name":"Outlined","scheme":"light","description":"Transparent surfaces drawn with crisp 1.5 px outlines.","tokens":{"background":"#ffffff","pageTint":"#f7f8fa","surface":"#ffffff","surfaceAlt":"#ffffff","text":"#17181c","muted":"#6e7079","accent":"#2457d6","accentText":"#ffffff","accentSoft":"#ffffff","border":"#cfd2d9","track":"#cfd2d9","mediaBg":"#ffffff","ok":"#16855a","warn":"#b7770b","danger":"#c93a3a","info":"#2457d6","radius":"10px","radiusSm":"8px","radiusXs":"6px","borderWidth":"1.5px","shadow":"none","shadowSm":"none","backdrop":"none","font":"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"$surface","color":"$text","border":"1.5px solid $text","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"$shadowSm"},"button.ghost":{"background":"transparent","color":"$accent","border":"none","borderRadius":"$radiusSm","fontWeight":"600","textDecoration":"underline"},"chip":{"background":"transparent","color":"$text","border":"1.5px solid $border","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1.5px solid $accent"},"tabs":{"background":"transparent","borderRadius":"$radiusSm","padding":"4px","border":"1.5px solid $border"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"$surface","color":"$text","boxShadow":"none","fontWeight":"600","border":"1.5px solid $text"},"toggle.track":{"background":"transparent","borderRadius":"999px","border":"1.5px solid $border"},"toggle.trackOn":{"background":"$accent","border":"1.5px solid $accent"},"toggle.knob":{"background":"$muted","borderRadius":"999px","boxShadow":"none"},"input":{"background":"$surface","color":"$muted","border":"1.5px solid $border","borderRadius":"$radiusSm"},"text.heading":{"color":"$text","fontSize":"24px","fontWeight":"700","letterSpacing":"-0.01em"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"$text","borderRadius":"$radius","boxShadow":"none","border":"1.5px solid $text"},"listrow":{"background":"transparent","color":"$text","borderBottom":"1px solid $border","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600","border":"1.5px solid $accent"},"pill":{"borderRadius":"999px","fontWeight":"600","fontSize":"12px"},"progress.track":{"background":"transparent","borderRadius":"999px","border":"1.5px solid $border"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"1.5px dashed $border"},"divider":{"background":"$border"},"icon":{"color":"$text"}}},{"id":"glass","name":"Glass","scheme":"light","description":"Frosted translucent panels over a soft gradient.","tokens":{"background":"linear-gradient(135deg, #dde8f8 0%, #ece3f6 52%, #def2ea 100%)","pageTint":"linear-gradient(135deg, #cfe0f7 0%, #d9e6fb 100%)","surface":"rgba(255,255,255,.55)","surfaceAlt":"rgba(255,255,255,.38)","text":"#1b2131","muted":"#5b6477","accent":"#4c66e8","accentText":"#ffffff","accentSoft":"rgba(76,102,232,.14)","border":"rgba(255,255,255,.75)","track":"rgba(27,33,49,.12)","mediaBg":"rgba(20,26,44,.82)","ok":"#178c60","warn":"#b67a0c","danger":"#cf3f4f","info":"#4c66e8","radius":"20px","radiusSm":"12px","radiusXs":"9px","borderWidth":"1px","shadow":"0 8px 32px rgba(31,38,80,.12)","shadowSm":"0 1px 3px rgba(31,38,80,.10)","backdrop":"blur(18px) saturate(1.4)","font":"-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"rgba(255,255,255,.6)","color":"$text","border":"1px solid $border","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"$shadowSm","backdropFilter":"$backdrop"},"button.ghost":{"background":"transparent","color":"$accent","border":"none","borderRadius":"$radiusSm","fontWeight":"600"},"chip":{"background":"rgba(255,255,255,.55)","color":"$text","border":"1px solid $border","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1px solid transparent"},"tabs":{"background":"rgba(255,255,255,.35)","borderRadius":"$radiusSm","padding":"4px","border":"1px solid $border","backdropFilter":"$backdrop"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"rgba(255,255,255,.85)","color":"$text","boxShadow":"$shadowSm","fontWeight":"600"},"toggle.track":{"background":"$track","borderRadius":"999px"},"toggle.trackOn":{"background":"$accent"},"toggle.knob":{"background":"#ffffff","borderRadius":"999px","boxShadow":"0 1px 2px rgba(0,0,0,.25)"},"input":{"background":"rgba(255,255,255,.5)","color":"$muted","border":"1px solid $border","borderRadius":"$radiusSm","backdropFilter":"$backdrop"},"text.heading":{"color":"$text","fontSize":"24px","fontWeight":"700","letterSpacing":"-0.01em"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"#ffffff","borderRadius":"$radius","boxShadow":"$shadow"},"listrow":{"background":"transparent","color":"$text","borderBottom":"1px solid rgba(27,33,49,.08)","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600"},"pill":{"borderRadius":"999px","fontWeight":"600","fontSize":"12px"},"progress.track":{"background":"$track","borderRadius":"999px"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"1px solid $border","backdropFilter":"$backdrop"},"divider":{"background":"$border"},"icon":{"color":"$text"}}},{"id":"minimal-mono","name":"Minimal mono","scheme":"light","description":"Black on white, monospace type, hairlines, colour only for status.","tokens":{"background":"#ffffff","pageTint":"#f6f6f4","surface":"#ffffff","surfaceAlt":"#f4f4f4","text":"#111111","muted":"#777777","accent":"#111111","accentText":"#ffffff","accentSoft":"#eeeeee","border":"#e2e2e2","track":"#e2e2e2","mediaBg":"#111111","ok":"#2f7d4f","warn":"#9a6b00","danger":"#b33a3a","info":"#111111","radius":"2px","radiusSm":"2px","radiusXs":"1px","borderWidth":"1px","shadow":"none","shadowSm":"none","backdrop":"none","font":"ui-monospace, 'SF Mono', 'Cascadia Mono', 'Cascadia Code', Consolas, 'Courier New', monospace","spacing":[4,8,12,16,24,32,48]},"components":{"card":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"card.title":{"fontSize":"15px","fontWeight":"600","color":"$text"},"button.primary":{"background":"$accent","color":"$accentText","border":"none","borderRadius":"$radiusSm","fontWeight":"600","boxShadow":"$shadowSm"},"button.secondary":{"background":"$surface","color":"$text","border":"1px solid $text","borderRadius":"$radiusSm","fontWeight":"500","boxShadow":"$shadowSm"},"button.ghost":{"background":"transparent","color":"$text","border":"none","borderRadius":"$radiusSm","fontWeight":"600","textDecoration":"underline"},"chip":{"background":"$surfaceAlt","color":"$text","border":"1px solid transparent","borderRadius":"999px","fontWeight":"500"},"chip.selected":{"background":"$accentSoft","color":"$accent","border":"1px solid transparent"},"tabs":{"background":"transparent","borderRadius":"0","padding":"4px","border":"none","borderBottom":"1px solid $border"},"tab":{"color":"$muted","borderRadius":"$radiusXs","fontWeight":"500"},"tab.active":{"background":"transparent","color":"$text","boxShadow":"none","fontWeight":"600","borderBottom":"2px solid $text","borderRadius":"0"},"toggle.track":{"background":"$track","borderRadius":"999px"},"toggle.trackOn":{"background":"$accent"},"toggle.knob":{"background":"#ffffff","borderRadius":"999px","boxShadow":"0 1px 2px rgba(0,0,0,.25)"},"input":{"background":"$surface","color":"$muted","border":"1px solid $border","borderRadius":"$radiusSm"},"text.heading":{"color":"$text","fontSize":"22px","fontWeight":"600","letterSpacing":"0"},"text.body":{"color":"$text","fontSize":"14px","fontWeight":"400"},"text.caption":{"color":"$muted","fontSize":"12px","fontWeight":"400"},"media":{"background":"$mediaBg","color":"#ffffff","borderRadius":"$radius","boxShadow":"$shadow"},"listrow":{"background":"transparent","color":"$text","borderBottom":"1px solid $border","borderRadius":"0"},"navrail":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","padding":"$sp1","backdropFilter":"$backdrop"},"navitem":{"color":"$muted","borderRadius":"$radiusSm","fontWeight":"500"},"navitem.active":{"background":"$accentSoft","color":"$accent","fontWeight":"600"},"pill":{"borderRadius":"999px","fontWeight":"500","fontSize":"12px"},"progress.track":{"background":"$track","borderRadius":"999px"},"progress.bar":{"background":"$accent","borderRadius":"999px"},"chart":{"background":"$surface","border":"$borderWidth solid $border","borderRadius":"$radius","boxShadow":"$shadow","color":"$text","padding":"$sp3","backdropFilter":"$backdrop"},"image":{"background":"$surfaceAlt","color":"$muted","borderRadius":"$radiusSm","border":"1px dashed $border"},"divider":{"background":"$border"},"icon":{"color":"$text"}}}]};
  /* END EMBEDDED LIBRARY */
  /* BEGIN EMBEDDED SAMPLE (copy of sample-project.json) */
  const EMBEDDED_SAMPLE = {"format":"nightshift-design-project","version":1,"name":"NightShift app (sample)","styleSetId":"bento-light","tint":true,"customSets":[],"pages":[{"id":"p-queue","name":"Queue","w":1366,"h":768,"styleSetId":null,"notes":"Home screen: what is rendering tonight and what needs my approval.","components":[{"id":"c001","type":"navrail","name":"Main navigation","label":"","x":24,"y":24,"w":88,"h":720,"options":{"items":"Queue\nCreate\nLibrary\nAssistant\nChannels\nResults\nSettings","active":0,"showLabels":true},"notes":"Switches between the app screens. Queue is highlighted.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c002","type":"text","name":"Page title","label":"Queue","x":136,"y":28,"w":320,"h":36,"options":{"variant":"heading","align":"left"},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c003","type":"text","name":"Page subtitle","label":"Tonight's renders, newest first","x":136,"y":66,"w":320,"h":20,"options":{"variant":"caption","align":"left"},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c004","type":"button","name":"Filters button","label":"Filters","x":1054,"y":32,"w":128,"h":40,"options":{"variant":"secondary"},"notes":"Opens the filter sheet (channel, status, date).","styleSetId":null,"overrides":{},"groupId":null},{"id":"c005","type":"button","name":"New render button","label":"New render","x":1198,"y":32,"w":144,"h":40,"options":{"variant":"primary"},"notes":"Goes to Create with an empty brief.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c006","type":"card","name":"Status strip","label":"","x":136,"y":104,"w":1206,"h":64,"options":{"showTitle":false},"notes":"Live counts for tonight's batch. Each pill filters the list below.","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c007","type":"pill","name":"Rendering count","label":"Rendering · 3","x":160,"y":124,"w":120,"h":24,"options":{"state":"info"},"notes":"","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c008","type":"pill","name":"Ready count","label":"Ready · 5","x":296,"y":124,"w":96,"h":24,"options":{"state":"ok"},"notes":"","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c009","type":"pill","name":"Needs review count","label":"Needs review · 2","x":408,"y":124,"w":144,"h":24,"options":{"state":"warn"},"notes":"","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c010","type":"pill","name":"Failed count","label":"Failed · 1","x":568,"y":124,"w":96,"h":24,"options":{"state":"danger"},"notes":"Click to see the error and retry.","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c011","type":"text","name":"Batch progress label","label":"Batch progress · 64 %","x":1000,"y":118,"w":318,"h":16,"options":{"variant":"caption","align":"left"},"notes":"","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c012","type":"progress","name":"Batch progress","label":"","x":1000,"y":142,"w":318,"h":8,"options":{"value":64},"notes":"Overall progress of tonight's batch.","styleSetId":null,"overrides":{},"groupId":"g-status"},{"id":"c013","type":"tabs","name":"Queue filter tabs","label":"","x":136,"y":192,"w":456,"h":40,"options":{"tabs":"All, Rendering, Ready, Needs review","active":0},"notes":"Filters the list. Same filters as the status pills.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c014","type":"card","name":"Queue list card","label":"Up next","x":136,"y":256,"w":640,"h":488,"options":{"showTitle":true},"notes":"Scrollable list of queued renders.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c015","type":"listrow","name":"Queue item 1","label":"Morning news recap","x":152,"y":300,"w":608,"h":64,"options":{"subtitle":"Short · 0:42 · Channel A","meta":"Rendering 72 %","leading":"icon","checked":false},"notes":"Click to preview on the right.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c016","type":"listrow","name":"Queue item 2","label":"Product teaser v3","x":152,"y":364,"w":608,"h":64,"options":{"subtitle":"Reel · 0:30 · Channel B","meta":"Ready","leading":"icon","checked":false},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c017","type":"listrow","name":"Queue item 3","label":"Weekly explainer","x":152,"y":428,"w":608,"h":64,"options":{"subtitle":"Long · 8:12 · Channel A","meta":"Needs review","leading":"icon","checked":false},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c018","type":"listrow","name":"Queue item 4","label":"Behind the scenes","x":152,"y":492,"w":608,"h":64,"options":{"subtitle":"Short · 0:55 · Channel C","meta":"Queued","leading":"icon","checked":false},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c019","type":"listrow","name":"Queue item 5","label":"Listener questions","x":152,"y":556,"w":608,"h":64,"options":{"subtitle":"Podcast clip · 2:10 · Channel B","meta":"Failed","leading":"icon","checked":false},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c020","type":"button","name":"Load more button","label":"Load more","x":152,"y":688,"w":608,"h":40,"options":{"variant":"ghost"},"notes":"Loads the next 20 items.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c021","type":"media","name":"Preview player","label":"Preview","x":800,"y":256,"w":544,"h":306,"options":{"ratio":"16:9"},"notes":"Plays the selected item. Space toggles play.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c022","type":"card","name":"Checklist card","label":"Before you approve","x":800,"y":584,"w":544,"h":160,"options":{"showTitle":true},"notes":"Owner ticks each line before approving.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c023","type":"listrow","name":"Check: captions","label":"Captions are correct","x":816,"y":620,"w":512,"h":32,"options":{"subtitle":"","meta":"","leading":"checkbox","checked":true},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c024","type":"listrow","name":"Check: thumbnail","label":"Thumbnail chosen","x":816,"y":652,"w":512,"h":32,"options":{"subtitle":"","meta":"","leading":"checkbox","checked":false},"notes":"","styleSetId":null,"overrides":{},"groupId":null},{"id":"c025","type":"toggle","name":"Auto-publish toggle","label":"Auto-publish","x":816,"y":700,"w":160,"h":28,"options":{"on":true},"notes":"When on, approved items publish at their scheduled time.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c026","type":"button","name":"Request changes button","label":"Request changes","x":1032,"y":696,"w":152,"h":36,"options":{"variant":"secondary"},"notes":"Sends the item back to Create with a comment box.","styleSetId":null,"overrides":{},"groupId":null},{"id":"c027","type":"button","name":"Approve button","label":"Approve","x":1196,"y":696,"w":132,"h":36,"options":{"variant":"primary"},"notes":"Approves the selected render and moves it to Results.","styleSetId":null,"overrides":{},"groupId":null}]},{"id":"p-create","name":"Create","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]},{"id":"p-library","name":"Library","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]},{"id":"p-assistant","name":"Assistant","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]},{"id":"p-channels","name":"Channels","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]},{"id":"p-results","name":"Results","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]},{"id":"p-settings","name":"Settings","w":1366,"h":768,"styleSetId":null,"notes":"","components":[]}],"activePageId":"p-queue"};
  /* END EMBEDDED SAMPLE */

  /* ================================================================== icons (inline SVG, 16 px grid) */
  const ICONS = {
    logo: '<path d="M3 13 13 3"/><path d="M3 9.5V3h6.5"/><circle cx="12.5" cy="12.5" r="1.25"/>',
    file: '<path d="M4 1.75h5l3 3v9.5H4z"/><path d="M9 1.75v3h3"/>',
    folder: '<path d="M1.75 3.75h4.5l1.5 1.5h6.5v7.5H1.75z"/>',
    save: '<path d="M8 2v8M4.75 6.75 8 10l3.25-3.25M2.5 12v1.75h11V12"/>',
    import: '<path d="M8 10V2M4.75 5.25 8 2l3.25 3.25M2.5 12v1.75h11V12"/>',
    export: '<path d="M9.5 2.5h4v4M13.5 2.5l-6 6M12 9.5v4H2.5V4h4"/>',
    undo: '<path d="M5.5 3.5l-3 3 3 3"/><path d="M2.75 6.5h6.5a3.5 3.5 0 0 1 0 7H6.5"/>',
    redo: '<path d="M10.5 3.5l3 3-3 3"/><path d="M13.25 6.5h-6.5a3.5 3.5 0 0 0 0 7H9.5"/>',
    sliders: '<path d="M2.5 4.5h6M11.5 4.5h2M2.5 11.5h2M7.5 11.5h6"/><circle cx="10" cy="4.5" r="1.5"/><circle cx="6" cy="11.5" r="1.5"/>',
    drop: '<path d="M8 1.75s4.25 4.6 4.25 7.75a4.25 4.25 0 0 1-8.5 0C3.75 6.35 8 1.75 8 1.75z"/>',
    grid: '<path d="M2.5 2.5h11v11h-11zM2.5 6.17h11M2.5 9.83h11M6.17 2.5v11M9.83 2.5v11"/>',
    magnet: '<path d="M3.5 2.5v5.5a4.5 4.5 0 0 0 9 0V2.5h-3V8a1.5 1.5 0 0 1-3 0V2.5z"/><path d="M3.5 5h3M9.5 5h3"/>',
    ruler: '<path d="M1.75 10.5 10.5 1.75l3.75 3.75-8.75 8.75z"/><path d="M5 7.25l1.5 1.5M7.25 5l1.5 1.5M9.5 2.75 11 4.25"/>',
    minus: '<path d="M3.5 8h9"/>',
    plus: '<path d="M8 3.5v9M3.5 8h9"/>',
    fit: '<path d="M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"/>',
    help: '<circle cx="8" cy="8" r="6.25"/><path d="M6.25 6.25a1.75 1.75 0 1 1 2.5 1.6c-.5.25-.75.6-.75 1.15v.25"/><circle cx="8" cy="11.25" r=".7" fill="currentColor" stroke="none"/>',
    more: '<circle cx="3.75" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="8" cy="8" r="1.1" fill="currentColor" stroke="none"/><circle cx="12.25" cy="8" r="1.1" fill="currentColor" stroke="none"/>',
    close: '<path d="M4 4l8 8M12 4l-8 8"/>',
    copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V3.5a1 1 0 0 0-1-1h-6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2"/>',
    paste: '<rect x="3" y="2.75" width="10" height="11.5" rx="1.5"/><path d="M6 2.75V2h4v.75M5.75 7h4.5M5.75 10h3"/>',
    trash: '<path d="M2.75 4.25h10.5M6.5 4.25V2.75h3v1.5M4 4.25l.75 9h6.5l.75-9"/>',
    front: '<rect x="5.5" y="5.5" width="8" height="8" rx="1" fill="currentColor" fill-opacity=".2"/><path d="M2.5 10.5v-8h8"/>',
    back: '<rect x="2.5" y="2.5" width="8" height="8" rx="1" fill="currentColor" fill-opacity=".2"/><path d="M13.5 5.5v8h-8"/>',
    group: '<rect x="1.75" y="1.75" width="12.5" height="12.5" rx="1.5" stroke-dasharray="2 2"/><rect x="4.5" y="4.5" width="4" height="4"/><rect x="7.5" y="7.5" width="4" height="4"/>',
    ungroup: '<rect x="2.5" y="2.5" width="5" height="5"/><rect x="8.5" y="8.5" width="5" height="5"/>',
    rename: '<path d="M3 13h3l7-7-3-3-7 7z"/>',
    page: '<rect x="2.5" y="3" width="11" height="10" rx="1.5"/>',
    left: '<path d="M10 3.5 5.5 8l4.5 4.5"/>',
    right: '<path d="M6 3.5l4.5 4.5L6 12.5"/>',
    chevron: '<path d="M4.5 6.25 8 9.75l3.5-3.5"/>',
    alignL: '<path d="M2.5 2v12"/><rect x="4.5" y="4" width="8" height="3"/><rect x="4.5" y="9" width="5" height="3"/>',
    alignCH: '<path d="M8 2v12"/><rect x="3.5" y="4" width="9" height="3"/><rect x="5" y="9" width="6" height="3"/>',
    alignR: '<path d="M13.5 2v12"/><rect x="3.5" y="4" width="8" height="3"/><rect x="6.5" y="9" width="5" height="3"/>',
    alignT: '<path d="M2 2.5h12"/><rect x="4" y="4.5" width="3" height="8"/><rect x="9" y="4.5" width="3" height="5"/>',
    alignCV: '<path d="M2 8h12"/><rect x="4" y="3.5" width="3" height="9"/><rect x="9" y="5" width="3" height="6"/>',
    alignB: '<path d="M2 13.5h12"/><rect x="4" y="3.5" width="3" height="8"/><rect x="9" y="6.5" width="3" height="5"/>',
    /* palette */
    card: '<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="2.5"/><path d="M4.5 6h5M4.5 8.5h7"/>',
    button: '<rect x="1.75" y="4.75" width="12.5" height="6.5" rx="2" fill="currentColor" fill-opacity=".14"/><path d="M5.5 8h5"/>',
    buttonOutline: '<rect x="1.75" y="4.75" width="12.5" height="6.5" rx="2"/><path d="M5.5 8h5"/>',
    buttonGhost: '<rect x="1.75" y="4.75" width="12.5" height="6.5" rx="2" stroke-dasharray="1.6 1.6"/><path d="M5.5 8h5"/>',
    chip: '<rect x="2.25" y="5" width="11.5" height="6" rx="3"/><path d="M6 8h4.5"/>',
    tabs: '<path d="M1.75 12.75h12.5M2.25 12.75V5.5h4v7.25M6.25 7.5h3.5v5.25M9.75 7.5h3.5v5.25"/>',
    toggle: '<rect x="1.75" y="4.75" width="12.5" height="6.5" rx="3.25"/><circle cx="11" cy="8" r="1.75" fill="currentColor"/>',
    input: '<rect x="1.75" y="4.25" width="12.5" height="7.5" rx="1.5"/><path d="M4.25 6.5v3"/>',
    heading: '<path d="M3.5 3v10M10.5 3v10M3.5 8h7"/>',
    body: '<path d="M2.5 4h11M2.5 7h11M2.5 10h11M2.5 13h7"/>',
    caption: '<path d="M2.5 8.5h8M2.5 11.5h5"/>',
    media: '<rect x="1.75" y="3.25" width="12.5" height="9.5" rx="1.5"/><path d="M6.75 6v4l3.5-2z" fill="currentColor"/>',
    mediaV: '<rect x="4.25" y="1.75" width="7.5" height="12.5" rx="1.5"/><path d="M6.75 6v4l3.5-2z" fill="currentColor"/>',
    listrow: '<circle cx="3.5" cy="4.5" r="1"/><circle cx="3.5" cy="8" r="1"/><circle cx="3.5" cy="11.5" r="1"/><path d="M6.5 4.5h7M6.5 8h7M6.5 11.5h7"/>',
    navrail: '<rect x="1.75" y="1.75" width="12.5" height="12.5" rx="2"/><path d="M5.75 1.75v12.5M3.25 4.5h1M3.25 7h1M3.25 9.5h1"/>',
    pill: '<rect x="1.75" y="5" width="12.5" height="6" rx="3"/><circle cx="4.75" cy="8" r="1" fill="currentColor"/><path d="M7 8h4.5"/>',
    progress: '<rect x="1.75" y="6.25" width="12.5" height="3.5" rx="1.75"/><path d="M3.5 8h5" stroke-width="2"/>',
    chartLine: '<path d="M1.75 13.25h12.5M2.5 11l3.5-4 3 2.5 4.5-5.5"/>',
    chartBars: '<path d="M1.75 13.25h12.5"/><rect x="3" y="8" width="2" height="5"/><rect x="7" y="4" width="2" height="9"/><rect x="11" y="6.5" width="2" height="6.5"/>',
    image: '<rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.5"/><circle cx="5.5" cy="6.25" r="1.25"/><path d="M1.75 11.5l3.75-3.5 3 2.5 2-1.75 3.75 3.25"/>',
    divider: '<path d="M1.75 8h12.5"/><path d="M4 4.5h8M4 11.5h8" opacity=".35"/>',
    iconPh: '<rect x="2.25" y="2.25" width="11.5" height="11.5" rx="2.5" stroke-dasharray="2 1.6"/><path d="M8 5l.9 2 2.1.2-1.6 1.4.5 2.1L8 9.6l-1.9 1.1.5-2.1L5 7.2l2.1-.2z"/>'
  };
  const GLYPHS = {
    home: '<path d="M2.5 7.25 8 2.75l5.5 4.5v6.5h-3.5v-4h-4v4H2.5z"/>',
    queue: '<rect x="2.25" y="2.75" width="11.5" height="3" rx="1"/><rect x="2.25" y="7.25" width="11.5" height="3" rx="1"/><path d="M2.25 13h7"/>',
    create: '<rect x="2.25" y="2.25" width="11.5" height="11.5" rx="2.5"/><path d="M8 5v6M5 8h6"/>',
    library: '<path d="M2 2.5h3v11H2zM6 2.5h3v11H6zM10 3.3l2.5-.6 2.2 10.4-2.5.6z"/>',
    chat: '<path d="M2.25 3.25h11.5v7.5H7.5l-3 2.5v-2.5H2.25z"/>',
    channels: '<circle cx="8" cy="8" r="1.5"/><path d="M5.2 5.2a4 4 0 0 0 0 5.6M10.8 5.2a4 4 0 0 1 0 5.6M3.2 3.2a6.8 6.8 0 0 0 0 9.6M12.8 3.2a6.8 6.8 0 0 1 0 9.6"/>',
    results: '<path d="M1.75 13.25h12.5"/><rect x="3" y="8" width="2" height="5"/><rect x="7" y="4" width="2" height="9"/><rect x="11" y="6.5" width="2" height="6.5"/>',
    settings: '<circle cx="8" cy="8" r="2"/><path d="M8 1.75v1.75M8 12.5v1.75M1.75 8h1.75M12.5 8h1.75M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2"/>',
    star: '<path d="M8 2l1.8 3.8 4.2.5-3.1 2.9.8 4.1L8 11.3l-3.7 2 .8-4.1L2 6.3l4.2-.5z"/>',
    play: '<path d="M5 3v10l8-5z" fill="currentColor"/>',
    check: '<path d="M3 8.5l3 3 7-7"/>',
    user: '<circle cx="8" cy="5.5" r="2.75"/><path d="M2.75 13.75a5.25 5.25 0 0 1 10.5 0"/>',
    bell: '<path d="M4 11V7a4 4 0 0 1 8 0v4l1.25 1.5H2.75zM6.5 14h3"/>',
    search: '<circle cx="7" cy="7" r="4.25"/><path d="M10.25 10.25 13.5 13.5"/>',
    heart: '<path d="M8 13.5S2 10 2 5.75A3 3 0 0 1 8 4.5a3 3 0 0 1 6 1.25C14 10 8 13.5 8 13.5z"/>',
    folder: '<path d="M1.75 3.75h4.5l1.5 1.5h6.5v7.5H1.75z"/>',
    image: ICONS.image,
    video: ICONS.media,
    mic: '<rect x="5.75" y="1.75" width="4.5" height="8" rx="2.25"/><path d="M3.5 7.75a4.5 4.5 0 0 0 9 0M8 12.25v2"/>',
    calendar: '<rect x="2.25" y="3.25" width="11.5" height="10.5" rx="1.5"/><path d="M2.25 6.5h11.5M5.5 1.75v3M10.5 1.75v3"/>'
  };
  const NAV_GLYPH = { queue: 'queue', home: 'home', create: 'create', new: 'create', library: 'library', assistant: 'chat', chat: 'chat', channels: 'channels', results: 'results', analytics: 'results', settings: 'settings', search: 'search', profile: 'user', account: 'user', notifications: 'bell', calendar: 'calendar' };
  const FALLBACK_GLYPHS = ['star', 'folder', 'user', 'bell', 'heart', 'calendar'];

  function svg(paths, size = 16, extra = '') {
    return `<svg width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"${extra}>${paths}</svg>`;
  }
  const icon = (name, size = 16) => svg(ICONS[name] || '', size);
  const glyph = (name, size = 16) => svg(GLYPHS[name] || GLYPHS.star, size);

  /* ================================================================== component catalogue */
  const TYPES = {
    card: { label: 'Card', icon: 'card', w: 320, h: 200, defLabel: 'Card title', options: { showTitle: true } },
    button: { label: 'Button', icon: 'button', w: 136, h: 40, defLabel: 'Button', options: { variant: 'primary' } },
    chip: { label: 'Chip / Badge', icon: 'chip', w: 88, h: 28, defLabel: 'Chip', options: { selected: false } },
    tabs: { label: 'Tabs', icon: 'tabs', w: 360, h: 40, defLabel: '', options: { tabs: 'Overview, Details, History', active: 0 } },
    toggle: { label: 'Toggle', icon: 'toggle', w: 160, h: 28, defLabel: 'Toggle', options: { on: true } },
    input: { label: 'Input', icon: 'input', w: 280, h: 40, defLabel: 'Type here…', options: { kind: 'text' } },
    text: { label: 'Text', icon: 'heading', w: 280, h: 32, defLabel: 'Heading', options: { variant: 'heading', align: 'left' } },
    media: { label: 'Media panel', icon: 'media', w: 384, h: 216, defLabel: 'Video', options: { ratio: '16:9' } },
    listrow: { label: 'List row', icon: 'listrow', w: 360, h: 56, defLabel: 'List item', options: { subtitle: 'Supporting text', meta: '', leading: 'icon', checked: false } },
    navrail: { label: 'Nav rail', icon: 'navrail', w: 88, h: 480, defLabel: '', options: { items: 'Home\nSearch\nLibrary\nSettings', active: 0, showLabels: true } },
    pill: { label: 'Status pill', icon: 'pill', w: 96, h: 24, defLabel: 'Ready', options: { state: 'ok' } },
    progress: { label: 'Progress bar', icon: 'progress', w: 240, h: 8, defLabel: '', options: { value: 60 } },
    chart: { label: 'Chart', icon: 'chartLine', w: 360, h: 220, defLabel: 'Chart title', options: { chartType: 'line' } },
    image: { label: 'Image', icon: 'image', w: 240, h: 160, defLabel: 'Image', options: {} },
    divider: { label: 'Divider', icon: 'divider', w: 320, h: 16, defLabel: '', options: { orientation: 'horizontal' } },
    icon: { label: 'Icon', icon: 'iconPh', w: 32, h: 32, defLabel: '', options: { glyph: 'star' } }
  };
  const PALETTE = [
    { title: 'Layout', items: [
      { type: 'card', name: 'Card' },
      { type: 'navrail', name: 'Nav rail' },
      { type: 'listrow', name: 'List row' },
      { type: 'divider', name: 'Divider' }
    ] },
    { title: 'Controls', items: [
      { type: 'button', name: 'Primary button', icon: 'button', options: { variant: 'primary' } },
      { type: 'button', name: 'Secondary button', icon: 'buttonOutline', options: { variant: 'secondary' } },
      { type: 'button', name: 'Ghost button', icon: 'buttonGhost', options: { variant: 'ghost' } },
      { type: 'tabs', name: 'Tabs' },
      { type: 'toggle', name: 'Toggle' },
      { type: 'input', name: 'Input' },
      { type: 'chip', name: 'Chip / Badge' }
    ] },
    { title: 'Text', items: [
      { type: 'text', name: 'Heading', icon: 'heading', label: 'Heading', w: 280, h: 32, options: { variant: 'heading' } },
      { type: 'text', name: 'Body text', icon: 'body', label: 'Body text. Describe what this area says.', w: 320, h: 40, options: { variant: 'body' } },
      { type: 'text', name: 'Caption', icon: 'caption', label: 'Caption', w: 200, h: 18, options: { variant: 'caption' } }
    ] },
    { title: 'Media & data', items: [
      { type: 'media', name: 'Media 16:9', icon: 'media', options: { ratio: '16:9' } },
      { type: 'media', name: 'Media 9:16', icon: 'mediaV', w: 216, h: 384, options: { ratio: '9:16' } },
      { type: 'image', name: 'Image' },
      { type: 'chart', name: 'Line chart', icon: 'chartLine', options: { chartType: 'line' } },
      { type: 'chart', name: 'Bar chart', icon: 'chartBars', options: { chartType: 'bars' } },
      { type: 'icon', name: 'Icon' }
    ] },
    { title: 'Status', items: [
      { type: 'pill', name: 'Status pill' },
      { type: 'progress', name: 'Progress bar' }
    ] }
  ];
  const OVERRIDES = [
    { key: 'background', label: 'Background', kind: 'color' },
    { key: 'color', label: 'Text colour', kind: 'color' },
    { key: 'accent', label: 'Accent', kind: 'color' },
    { key: 'borderColor', label: 'Border colour', kind: 'color' },
    { key: 'radius', label: 'Corner radius (px)', kind: 'number' },
    { key: 'fontSize', label: 'Font size (px)', kind: 'number' },
    { key: 'shadow', label: 'Shadow', kind: 'select', options: [['', 'Style default'], ['none', 'None'], ['soft', 'Soft'], ['strong', 'Strong']] }
  ];
  const SHADOWS = { none: 'none', soft: '0 2px 8px rgba(0,0,0,.08), 0 12px 32px rgba(0,0,0,.08)', strong: '0 6px 16px rgba(0,0,0,.16), 0 24px 56px rgba(0,0,0,.18)' };
  const PILL_STATES = [['ok', 'Success'], ['warn', 'Warning'], ['danger', 'Error'], ['info', 'Info'], ['neutral', 'Neutral']];

  /* ================================================================== state */
  let library = { sets: [] };
  let project = null;
  let selection = new Set();
  let zoom = 1;
  const ui = { grid: true, snap: true, rulers: false };
  let undoStack = [];
  let redoStack = [];
  let lastSnap = '';
  let lastCommit = { tag: null, time: 0 };
  let clipboard = null;
  let previewSet = null;
  let spaceHeld = false;
  let drag = null;
  let hoverId = null;
  let modalState = null;
  let savedAt = null;
  let cursor = null;

  /* ================================================================== helpers */
  const $ = (s, r = document) => r.querySelector(s);
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const snapV = (v) => Math.round(v / GRID) * GRID;
  const uid = () => Math.random().toString(36).slice(2, 10);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 0) => { const n = parseFloat(v); return Number.isFinite(n) ? n : d; };
  const splitList = (s) => String(s || '').split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

  function el(tag, props, ...children) {
    const e = document.createElement(tag);
    let value;
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') e.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(e.style, v);
      else if (k === 'html') e.innerHTML = v;
      else if (k === 'value') value = v;
      else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
      else if (typeof v === 'boolean' || typeof v === 'number') { if (k in e) e[k] = v; else e.setAttribute(k, String(v)); }
      else e.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      e.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    if (value !== undefined) e.value = value;
    return e;
  }

  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => t.classList.remove('on'), 1600);
  }

  function download(name, text, type = 'application/json') {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const a = el('a', { href: url, download: name });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      const ta = el('textarea', { style: { position: 'fixed', left: '-9999px' } });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
      return ok;
    }
  }

  const slug = (s) => String(s || 'project').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'project';

  function hash(str) {
    let h = 2166136261;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }

  /* ================================================================== model */
  function newPage(name, size) {
    return { id: 'p-' + uid(), name, w: size ? size.w : 1366, h: size ? size.h : 768, styleSetId: null, notes: '', components: [] };
  }
  function newProjectData() {
    const pages = DEFAULT_PAGE_NAMES.map((n) => newPage(n));
    return { format: 'nightshift-design-project', version: 1, name: 'Untitled app', styleSetId: 'bento-light', tint: false, customSets: [], pages, activePageId: pages[0].id };
  }
  function normalizeComp(c) {
    if (!c || !TYPES[c.type]) return null;
    const T = TYPES[c.type];
    return {
      id: String(c.id || uid()),
      type: c.type,
      name: String(c.name || T.label),
      label: c.label == null ? T.defLabel : String(c.label),
      x: Math.round(num(c.x)), y: Math.round(num(c.y)),
      w: Math.max(1, Math.round(num(c.w ?? c.width, T.w))), h: Math.max(1, Math.round(num(c.h ?? c.height, T.h))),
      options: Object.assign(clone(T.options), c.options || {}),
      notes: String(c.notes || ''),
      styleSetId: c.styleSetId || null,
      overrides: c.overrides && typeof c.overrides === 'object' ? c.overrides : {},
      groupId: c.groupId || null
    };
  }
  function normalizeProject(p) {
    const out = newProjectData();
    if (!p || typeof p !== 'object') return out;
    out.name = String(p.name || out.name);
    out.styleSetId = p.styleSetId || out.styleSetId;
    out.tint = !!p.tint;
    out.customSets = Array.isArray(p.customSets) ? p.customSets.filter((s) => s && s.id && s.tokens) : [];
    if (Array.isArray(p.pages) && p.pages.length) {
      const seen = new Set();
      out.pages = p.pages.map((pg) => {
        let id = String(pg.id || 'p-' + uid());
        if (seen.has(id)) id = 'p-' + uid();
        seen.add(id);
        const ids = new Set();
        const components = (pg.components || []).map(normalizeComp).filter(Boolean).map((c) => {
          if (ids.has(c.id)) c.id = uid();
          ids.add(c.id);
          return c;
        });
        return { id, name: String(pg.name || 'Page'), w: Math.max(100, Math.round(num(pg.w ?? pg.width, 1366))), h: Math.max(100, Math.round(num(pg.h ?? pg.height, 768))), styleSetId: pg.styleSetId || null, notes: String(pg.notes || ''), components };
      });
    }
    out.activePageId = out.pages.some((pg) => pg.id === p.activePageId) ? p.activePageId : out.pages[0].id;
    return out;
  }
  const page = () => project.pages.find((p) => p.id === project.activePageId) || project.pages[0];
  const comps = () => page().components;
  const byId = (id) => comps().find((c) => c.id === id);
  const selected = () => comps().filter((c) => selection.has(c.id));
  function bbox(list) {
    if (!list.length) return null;
    let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
    for (const c of list) { x1 = Math.min(x1, c.x); y1 = Math.min(y1, c.y); x2 = Math.max(x2, c.x + c.w); y2 = Math.max(y2, c.y + c.h); }
    return { x: x1, y: y1, w: x2 - x1, h: y2 - y1 };
  }
  function expandGroups(ids) {
    const out = new Set(ids);
    const gids = new Set(comps().filter((c) => out.has(c.id) && c.groupId).map((c) => c.groupId));
    if (gids.size) comps().forEach((c) => { if (c.groupId && gids.has(c.groupId)) out.add(c.id); });
    return out;
  }
  function uniqueName(base) {
    const names = new Set(comps().map((c) => c.name));
    if (!names.has(base)) return base;
    let i = 2;
    while (names.has(`${base} ${i}`)) i++;
    return `${base} ${i}`;
  }

  /* ================================================================== styles */
  const allSets = () => [...library.sets, ...(project ? project.customSets : [])];
  function getSet(id) {
    if (previewSet && previewSet.targetId === id) return previewSet.set;
    const sets = allSets();
    return sets.find((s) => s.id === id) || sets[0];
  }
  const pageSetId = (pg) => pg.styleSetId || project.styleSetId;
  const currentSet = () => getSet(pageSetId(page()));
  const ruleCache = new WeakMap();
  function rule(set, key) {
    const src = set.components || {};
    let m = ruleCache.get(src);
    if (!m) { m = {}; ruleCache.set(src, m); }
    if (!(key in m)) {
      const fb = library.sets[0] && library.sets[0].components ? library.sets[0].components[key] : null;
      const r = src[key] || fb || {};
      const o = {};
      for (const [k, v] of Object.entries(r)) o[k] = String(v).replace(/\$([A-Za-z][A-Za-z0-9]*)/g, (_, n) => `var(--t-${n})`);
      m[key] = o;
    }
    return m[key];
  }
  function applyTokens(node, set) {
    const base = library.sets[0] ? library.sets[0].tokens : {};
    const t = Object.assign({}, base, set.tokens || {});
    for (const [k, v] of Object.entries(t)) {
      if (k === 'spacing') {
        (Array.isArray(v) ? v : splitList(v)).forEach((s, i) => node.style.setProperty(`--t-sp${i}`, /^\d+(\.\d+)?$/.test(String(s)) ? s + 'px' : String(s)));
      } else node.style.setProperty(`--t-${k}`, String(v));
    }
  }
  function applyOverrides(e, c) {
    const ov = c.overrides || {};
    if (ov.accent) { e.style.setProperty('--t-accent', ov.accent); e.style.setProperty('--t-accentSoft', `color-mix(in srgb, ${ov.accent} 15%, transparent)`); }
    if (ov.borderColor) e.style.setProperty('--t-border', ov.borderColor);
    if (ov.radius !== undefined && ov.radius !== '' && ov.radius !== null) {
      const r = num(ov.radius) + 'px';
      ['radius', 'radiusSm', 'radiusXs'].forEach((k) => e.style.setProperty('--t-' + k, r));
    }
    if (ov.color) { e.style.setProperty('--t-text', ov.color); e.style.color = ov.color; }
    if (ov.background) {
      const line = e.querySelector(':scope > .line');
      (line || e).style.background = ov.background;
    }
    if (ov.fontSize) e.style.fontSize = num(ov.fontSize, 14) + 'px';
    if (ov.shadow && SHADOWS[ov.shadow]) {
      e.style.setProperty('--t-shadow', SHADOWS[ov.shadow]);
      e.style.setProperty('--t-shadowSm', SHADOWS[ov.shadow]);
      e.style.boxShadow = SHADOWS[ov.shadow];
    }
  }

  /* ================================================================== rendering components */
  function navGlyph(label, i) {
    const k = String(label).toLowerCase().trim();
    return NAV_GLYPH[k] || FALLBACK_GLYPHS[i % FALLBACK_GLYPHS.length];
  }

  function chartSvg(c) {
    const type = c.options.chartType === 'bars' ? 'bars' : 'line';
    const seed = hash(c.id);
    const pts = Array.from({ length: 8 }, (_, i) => 18 + ((seed >> (i * 3)) % 32) + i * 3);
    const W = 100, H = 60;
    const grid = [15, 30, 45].map((y) => `<line x1="0" x2="${W}" y1="${y}" y2="${y}" stroke="var(--t-border)" stroke-width="1" vector-effect="non-scaling-stroke"/>`).join('');
    let body = '';
    if (type === 'line') {
      const xy = pts.map((v, i) => [(i / (pts.length - 1)) * W, H - (v / 64) * H]);
      const d = xy.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(2) + ' ' + p[1].toFixed(2)).join(' ');
      body = `<path d="${d} L${W} ${H} L0 ${H} Z" fill="var(--t-accent)" fill-opacity=".12" stroke="none"/>` +
        `<path d="${d}" fill="none" stroke="var(--t-accent)" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`;
    } else {
      const n = pts.length, slot = W / n;
      body = pts.map((v, i) => { const bh = (v / 64) * H; return `<rect x="${(i * slot + slot * 0.2).toFixed(2)}" y="${(H - bh).toFixed(2)}" width="${(slot * 0.6).toFixed(2)}" height="${bh.toFixed(2)}" fill="var(--t-accent)" opacity="${i === n - 1 ? 1 : 0.75}"/>`; }).join('');
    }
    const base = `<line x1="0" x2="${W}" y1="${H}" y2="${H}" stroke="var(--t-muted)" stroke-opacity=".5" stroke-width="1" vector-effect="non-scaling-stroke"/>`;
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" width="100%" height="100%">${grid}${body}${base}</svg>`;
  }

  function renderComp(c, pageSet) {
    const set = c.styleSetId ? getSet(c.styleSetId) : pageSet;
    const o = c.options || {};
    const e = el('div', { class: 'cmp cmp-' + c.type, 'data-id': c.id });
    Object.assign(e.style, { left: c.x + 'px', top: c.y + 'px', width: c.w + 'px', height: c.h + 'px' });
    if (c.styleSetId) { applyTokens(e, set); e.style.fontFamily = 'var(--t-font)'; e.style.color = 'var(--t-text)'; }
    const css = (node, k) => { Object.assign(node.style, rule(set, k)); return node; };
    switch (c.type) {
      case 'card':
        css(e, 'card');
        if (o.showTitle !== false && c.label) e.append(css(el('div', { class: 'cmp-card-title' }, c.label), 'card.title'));
        break;
      case 'button':
        e.classList.add('cmp-flex');
        css(e, 'button.' + (['primary', 'secondary', 'ghost'].includes(o.variant) ? o.variant : 'primary'));
        e.append(el('span', { class: 'ell' }, c.label));
        break;
      case 'chip':
        e.classList.add('cmp-flex');
        css(e, 'chip');
        if (o.selected) css(e, 'chip.selected');
        e.append(el('span', { class: 'ell' }, c.label));
        break;
      case 'tabs': {
        css(e, 'tabs');
        const labels = splitList(o.tabs);
        const active = clamp(Math.round(num(o.active)), 0, Math.max(0, labels.length - 1));
        labels.forEach((l, i) => {
          const t = css(el('div', {}, el('span', { class: 'ell' }, l)), 'tab');
          if (i === active) css(t, 'tab.active');
          e.append(t);
        });
        break;
      }
      case 'toggle': {
        const th = clamp(Math.min(c.h, 24), 12, 40);
        const tw = Math.round(th * 1.75);
        const ks = th - 4;
        const track = css(el('div', { class: 'track', style: { width: tw + 'px', height: th + 'px' } }), 'toggle.track');
        if (o.on) css(track, 'toggle.trackOn');
        track.append(css(el('div', { class: 'knob', style: { width: ks + 'px', height: ks + 'px', left: (o.on ? tw - ks - 2 : 2) + 'px', top: '2px' } }), 'toggle.knob'));
        Object.assign(track.style, { boxSizing: 'content-box' });
        e.append(track);
        if (c.label) e.append(el('span', { class: 'ell', style: { color: 'var(--t-text)' } }, c.label));
        break;
      }
      case 'input':
        css(e, 'input');
        if (o.kind === 'search') e.append(el('span', { html: glyph('search', 16), style: { opacity: '.7' } }));
        e.append(el('span', { class: 'ell', style: { flex: '1' } }, c.label));
        if (o.kind === 'dropdown') e.append(el('span', { html: icon('chevron', 16), style: { opacity: '.7' } }));
        break;
      case 'text':
        css(e, 'text.' + (['heading', 'body', 'caption'].includes(o.variant) ? o.variant : 'body'));
        e.style.textAlign = o.align || 'left';
        e.textContent = c.label;
        break;
      case 'media': {
        css(e, 'media');
        const s = clamp(Math.min(c.w, c.h) * 0.22, 28, 72);
        e.append(el('div', { class: 'play', style: { width: s + 'px', height: s + 'px', background: 'color-mix(in srgb, currentColor 16%, transparent)' }, html: glyph('play', Math.round(s * 0.42)) }));
        if (c.label) e.append(el('div', { class: 'cap' }, c.label));
        e.append(el('div', { class: 'ratio' }, o.ratio === '9:16' ? '9:16' : '16:9'));
        break;
      }
      case 'listrow': {
        css(e, 'listrow');
        const ls = clamp(c.h - 20, 16, 40);
        if (o.leading === 'icon') {
          e.append(el('div', { class: 'lead', style: { width: ls + 'px', height: ls + 'px', background: 'var(--t-surfaceAlt)', borderRadius: 'var(--t-radiusXs)', color: 'var(--t-muted)' }, html: glyph(o.glyph || 'video', Math.round(ls * 0.5)) }));
        } else if (o.leading === 'avatar') {
          e.append(el('div', { class: 'lead', style: { width: ls + 'px', height: ls + 'px', borderRadius: '50%', background: 'var(--t-accentSoft)', color: 'var(--t-accent)', fontSize: Math.round(ls * 0.4) + 'px', fontWeight: '600' } }, (c.label || '?').trim().slice(0, 1).toUpperCase()));
        } else if (o.leading === 'checkbox') {
          const on = !!o.checked;
          e.append(el('div', { class: 'lead', style: { width: '18px', height: '18px', borderRadius: '5px', border: on ? 'none' : '1.5px solid var(--t-muted)', background: on ? 'var(--t-accent)' : 'transparent', color: 'var(--t-accentText)' }, html: on ? glyph('check', 12) : '' }));
        }
        const body = el('div', { class: 'body' }, el('div', { class: 't ell' }, c.label));
        if (o.subtitle) body.append(el('div', { class: 's ell' }, o.subtitle));
        e.append(body);
        if (o.meta) e.append(el('div', { class: 'm' }, o.meta));
        break;
      }
      case 'navrail': {
        css(e, 'navrail');
        const wide = c.w >= 150;
        if (wide) e.classList.add('wide');
        const items = splitList(o.items);
        const active = Math.round(num(o.active));
        const ih = wide ? 40 : (o.showLabels !== false ? 56 : 44);
        items.forEach((label, i) => {
          const it = css(el('div', { class: 'item', style: { height: ih + 'px' } }), 'navitem');
          if (i === active) css(it, 'navitem.active');
          it.insertAdjacentHTML('beforeend', glyph(navGlyph(label, i), wide ? 16 : 20));
          if (o.showLabels !== false) it.append(el('span', { class: 'ell' }, label));
          it.title = label;
          e.append(it);
        });
        break;
      }
      case 'pill': {
        css(e, 'pill');
        const col = o.state === 'neutral' || !o.state ? 'var(--t-muted)' : `var(--t-${o.state})`;
        e.style.color = col;
        e.style.background = `color-mix(in srgb, ${col} 14%, transparent)`;
        e.append(el('span', { class: 'dot' }), el('span', { class: 'ell' }, c.label));
        break;
      }
      case 'progress':
        css(e, 'progress.track');
        e.append(css(el('div', { class: 'bar', style: { width: clamp(num(o.value), 0, 100) + '%' } }), 'progress.bar'));
        break;
      case 'chart':
        css(e, 'chart');
        if (c.label) e.append(el('div', { class: 'ct ell' }, c.label));
        e.append(el('div', { style: { flex: '1', minHeight: '0' }, html: chartSvg(c) }));
        break;
      case 'image':
        css(e, 'image');
        e.insertAdjacentHTML('beforeend', svg(ICONS.image, clamp(Math.min(c.w, c.h) * 0.25, 16, 40)));
        if (c.label && c.h >= 60) e.append(el('div', { class: 'ell' }, c.label));
        break;
      case 'divider': {
        const vertical = o.orientation === 'vertical';
        const line = css(el('div', { class: 'line', style: vertical ? { top: '0', bottom: '0', left: '50%', width: '1px' } : { left: '0', right: '0', top: '50%', height: '1px' } }), 'divider');
        e.append(line);
        break;
      }
      case 'icon':
        css(e, 'icon');
        e.innerHTML = glyph(o.glyph || 'star', Math.max(8, Math.round(Math.min(c.w, c.h) * 0.75)));
        break;
      default:
        break;
    }
    applyOverrides(e, c);
    return e;
  }

  /* ================================================================== DOM refs */
  const viewport = $('#viewport');
  const stage = $('#stage');
  const pageWrap = $('#pageWrap');
  const pageEl = $('#page');
  const overlay = $('#overlay');
  const gridLayer = $('#gridLayer');
  const canvasArea = $('#canvasArea');

  /* ================================================================== canvas rendering */
  function renderCanvas() {
    const pg = page();
    const set = currentSet();
    pageEl.removeAttribute('style');
    applyTokens(pageEl, set);
    Object.assign(pageEl.style, {
      width: pg.w + 'px',
      height: pg.h + 'px',
      transform: `scale(${zoom})`,
      background: project.tint && set.tokens && set.tokens.pageTint ? 'var(--t-pageTint)' : 'var(--t-background)',
      color: 'var(--t-text)',
      fontFamily: 'var(--t-font)'
    });
    pageEl.replaceChildren(...pg.components.map((c) => renderComp(c, set)));
    pageWrap.style.width = Math.round(pg.w * zoom) + 'px';
    pageWrap.style.height = Math.round(pg.h * zoom) + 'px';
    renderGrid(set);
    $('#emptyHint').classList.toggle('on', pg.components.length === 0);
    renderOverlay();
    drawRulers();
    renderStatus();
  }
  function renderOne(c) {
    const node = pageEl.querySelector(`[data-id="${CSS.escape(c.id)}"]`);
    if (node) node.replaceWith(renderComp(c, currentSet()));
  }
  function renderGrid(set) {
    gridLayer.classList.toggle('on', ui.grid);
    if (!ui.grid) return;
    const dark = set.scheme === 'dark';
    const minor = dark ? 'rgba(255,255,255,.035)' : 'rgba(0,0,0,.03)';
    const major = dark ? 'rgba(255,255,255,.07)' : 'rgba(0,0,0,.06)';
    let s = GRID * zoom;
    while (s < 7) s *= 2;
    const M = GRID * 8 * zoom;
    gridLayer.style.backgroundImage = [
      `linear-gradient(to right, ${major} 1px, transparent 1px)`, `linear-gradient(to bottom, ${major} 1px, transparent 1px)`,
      `linear-gradient(to right, ${minor} 1px, transparent 1px)`, `linear-gradient(to bottom, ${minor} 1px, transparent 1px)`
    ].join(',');
    gridLayer.style.backgroundSize = `${M}px ${M}px, ${M}px ${M}px, ${s}px ${s}px, ${s}px ${s}px`;
  }

  function boxNode(cls, r) {
    const z = zoom;
    return el('div', { class: cls, style: { left: Math.round(r.x * z) - 0.5 + 'px', top: Math.round(r.y * z) - 0.5 + 'px', width: Math.round(r.w * z) + 1 + 'px', height: Math.round(r.h * z) + 1 + 'px' } });
  }
  function renderOverlay(guides) {
    overlay.replaceChildren();
    const z = zoom;
    if (hoverId && !selection.has(hoverId) && !drag) {
      const c = byId(hoverId);
      if (c) overlay.append(boxNode('hover-box', c));
    }
    const sel = selected();
    if (sel.length) {
      const bb = bbox(sel);
      const isGroup = sel.length > 1 && sel.every((c) => c.groupId && c.groupId === sel[0].groupId);
      if (sel.length > 1) sel.forEach((c) => overlay.append(boxNode('sel-box member', c)));
      overlay.append(boxNode('sel-box' + (isGroup ? ' group' : ''), bb));
      const label = sel.length === 1 ? sel[0].name : (isGroup ? 'Group · ' : '') + sel.length + ' items';
      overlay.append(el('div', { class: 'name-tag', style: { left: Math.round(bb.x * z) + 'px', top: Math.round(bb.y * z) - 3 + 'px' } }, label));
      overlay.append(el('div', { class: 'size-tag', style: { left: Math.round((bb.x + bb.w / 2) * z) + 'px', top: Math.round((bb.y + bb.h) * z) + 7 + 'px' } }, `${Math.round(bb.w)} × ${Math.round(bb.h)}`));
      if (!drag || drag.mode !== 'move') {
        const xs = { w: bb.x, c: bb.x + bb.w / 2, e: bb.x + bb.w };
        const ys = { n: bb.y, c: bb.y + bb.h / 2, s: bb.y + bb.h };
        for (const d of DIRS) {
          const hx = d.includes('w') ? xs.w : d.includes('e') ? xs.e : xs.c;
          const hy = d.includes('n') ? ys.n : d.includes('s') ? ys.s : ys.c;
          overlay.append(el('div', { class: 'handle', 'data-dir': d, style: { left: Math.round(hx * z) + 'px', top: Math.round(hy * z) + 'px' } }));
        }
      }
    }
    (guides || []).forEach((g) => overlay.append(el('div', { class: 'guide ' + (g.axis === 'x' ? 'v' : 'h'), style: g.axis === 'x' ? { left: Math.round(g.pos * z) + 'px' } : { top: Math.round(g.pos * z) + 'px' } })));
    if (drag && drag.mode === 'marquee' && drag.rect) overlay.append(boxNode('marquee', drag.rect));
  }

  /* ---------------- rulers */
  function drawRulers() {
    canvasArea.classList.toggle('rulers', ui.rulers);
    if (!ui.rulers) return;
    const vr = viewport.getBoundingClientRect();
    const pr = pageWrap.getBoundingClientRect();
    const sel = selected();
    const bb = sel.length ? bbox(sel) : null;
    drawRuler($('#rulerTop'), 'h', vr.width, pr.left - vr.left, page().w, bb ? [bb.x, bb.x + bb.w] : null);
    drawRuler($('#rulerLeft'), 'v', vr.height, pr.top - vr.top, page().h, bb ? [bb.y, bb.y + bb.h] : null);
  }
  function drawRuler(cv, dir, len, offset, extent, range) {
    const R = 20;
    const dpr = window.devicePixelRatio || 1;
    const W = dir === 'h' ? len : R;
    const H = dir === 'h' ? R : len;
    cv.width = Math.max(1, Math.round(W * dpr));
    cv.height = Math.max(1, Math.round(H * dpr));
    cv.style.width = W + 'px';
    cv.style.height = H + 'px';
    const g = cv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.fillStyle = '#fafafa';
    g.fillRect(0, 0, W, H);
    const along = (a, b) => (dir === 'h' ? [a, 0, b - a, R] : [0, a, R, b - a]);
    g.fillStyle = '#ffffff';
    g.fillRect(...along(offset, offset + extent * zoom));
    if (range) { g.fillStyle = 'rgba(37,99,235,.14)'; g.fillRect(...along(offset + range[0] * zoom, offset + range[1] * zoom)); }
    let major = 50;
    for (const m of [10, 20, 50, 100, 200, 500]) { major = m; if (m * zoom >= 56) break; }
    const minor = major / 10 * zoom >= 5 ? major / 10 : major / 5;
    const start = Math.floor(-offset / zoom / minor) * minor;
    const end = (len - offset) / zoom;
    g.strokeStyle = '#c8c8ce';
    g.fillStyle = '#7a7a83';
    g.font = '10px ' + getComputedStyle(document.body).fontFamily;
    g.lineWidth = 1;
    g.beginPath();
    for (let v = start; v <= end; v += minor) {
      const vv = Math.round(v * 1000) / 1000;
      const pos = Math.round(offset + vv * zoom) + 0.5;
      const isMajor = Math.abs(vv / major - Math.round(vv / major)) < 1e-6;
      const half = Math.abs((vv * 2) / major - Math.round((vv * 2) / major)) < 1e-6;
      const t = isMajor ? R : half ? 8 : 4;
      if (dir === 'h') { g.moveTo(pos, R); g.lineTo(pos, R - t); } else { g.moveTo(R, pos); g.lineTo(R - t, pos); }
      if (isMajor) {
        if (dir === 'h') g.fillText(String(Math.round(vv)), pos + 3, 10);
        else { g.save(); g.translate(10, pos - 3); g.rotate(-Math.PI / 2); g.fillText(String(Math.round(vv)), 0, 0); g.restore(); }
      }
    }
    g.stroke();
  }

  /* ---------------- status bar */
  function renderStatus() {
    const pg = page();
    const sel = selected();
    const parts = [];
    parts.push(el('span', {}, el('b', {}, pg.name), ` · ${pg.w} × ${pg.h} · ${pg.components.length} component${pg.components.length === 1 ? '' : 's'}`));
    if (sel.length === 1) {
      const c = sel[0];
      parts.push(el('span', {}, el('b', {}, c.name), ` · ${TYPES[c.type].label} · x ${c.x}, y ${c.y} · ${c.w} × ${c.h}`));
    } else if (sel.length > 1) {
      const bb = bbox(sel);
      parts.push(el('span', {}, el('b', {}, `${sel.length} selected`), ` · x ${Math.round(bb.x)}, y ${Math.round(bb.y)} · ${Math.round(bb.w)} × ${Math.round(bb.h)}`));
    }
    parts.push(el('span', { class: 'grow' }));
    if (cursor) parts.push(el('span', {}, `x ${Math.round(cursor.x)}  y ${Math.round(cursor.y)}`));
    parts.push(el('span', {}, `Style: ${currentSet().name}`));
    parts.push(el('span', {}, savedAt ? `Autosaved ${savedAt}` : 'Autosave on'));
    $('#statusbar').replaceChildren(...parts);
  }

  /* ================================================================== history & persistence */
  function snapshot() {
    const { activePageId, ...rest } = project;
    return JSON.stringify(rest);
  }
  function commit(tag) {
    const now = snapshot();
    if (now === lastSnap) return;
    const t = Date.now();
    if (!(tag && lastCommit.tag === tag && t - lastCommit.time < 900 && undoStack.length)) {
      undoStack.push({ snap: lastSnap, pageId: project.activePageId });
      if (undoStack.length > MAX_HISTORY) undoStack.shift();
    }
    lastCommit = { tag: tag || null, time: t };
    redoStack = [];
    lastSnap = now;
    persist();
    renderPagebar();
    renderToolbar();
  }
  function restore(entry) {
    const data = JSON.parse(entry.snap);
    project = normalizeProject(Object.assign(data, { activePageId: entry.pageId }));
    lastSnap = snapshot();
    const ids = new Set(comps().map((c) => c.id));
    selection = new Set([...selection].filter((id) => ids.has(id)));
    persist();
    renderAll();
  }
  function undo() {
    if (snapshot() !== lastSnap) commit();
    if (!undoStack.length) return toast('Nothing to undo');
    const entry = undoStack.pop();
    redoStack.push({ snap: lastSnap, pageId: entry.pageId });
    lastCommit = { tag: null, time: 0 };
    restore(entry);
    toast('Undo');
  }
  function redo() {
    if (!redoStack.length) return toast('Nothing to redo');
    const entry = redoStack.pop();
    undoStack.push({ snap: lastSnap, pageId: entry.pageId });
    lastCommit = { tag: null, time: 0 };
    restore(entry);
    toast('Redo');
  }
  function persist() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(project));
      savedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (err) {
      savedAt = null;
    }
    renderStatus();
  }
  function saveUi() {
    try { localStorage.setItem(UI_KEY, JSON.stringify(ui)); } catch (err) { /* private mode: ignore */ }
  }
  function loadUi() {
    try { Object.assign(ui, JSON.parse(localStorage.getItem(UI_KEY) || '{}')); } catch (err) { /* ignore */ }
  }
  function replaceProject(p, message) {
    if (snapshot() !== lastSnap) commit();
    undoStack.push({ snap: lastSnap, pageId: project.activePageId });
    if (undoStack.length > MAX_HISTORY) undoStack.shift();
    redoStack = [];
    project = normalizeProject(p);
    lastSnap = snapshot();
    selection.clear();
    persist();
    renderAll();
    requestAnimationFrame(zoomFit);
    if (message) toast(message);
  }

  /* ================================================================== top-level render */
  function renderAll() {
    renderToolbar();
    renderPagebar();
    renderCanvas();
    renderInspector();
  }
  function refresh() {
    renderCanvas();
  }
  function renderToolbar() {
    const nameInput = $('#projectName');
    if (document.activeElement !== nameInput) nameInput.value = project.name;
    const sel = $('#styleSelect');
    sel.replaceChildren(el('optgroup', { label: 'Style library' }, library.sets.map((s) => el('option', { value: s.id }, s.name))), project.customSets.length ? el('optgroup', { label: 'Your sets' }, project.customSets.map((s) => el('option', { value: s.id }, s.name))) : null);
    sel.value = getSet(project.styleSetId).id;
    document.querySelectorAll('[data-toggle]').forEach((b) => {
      const k = b.dataset.toggle;
      const on = k === 'tint' ? !!project.tint : !!ui[k];
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', String(on));
    });
    $('[data-action="undo"]').disabled = !undoStack.length && snapshot() === lastSnap;
    $('[data-action="redo"]').disabled = !redoStack.length;
    $('#zoomLabel').textContent = Math.round(zoom * 100) + '%';
  }

  /* ---------------- page bar */
  function renderPagebar() {
    const bar = $('#pagebar');
    bar.replaceChildren();
    project.pages.forEach((pg) => {
      const active = pg.id === project.activePageId;
      const tab = el('div', { class: 'page-tab' + (active ? ' active' : ''), role: 'tab', tabindex: '0', 'aria-selected': String(active), title: 'Click to open · double-click to rename', 'data-page': pg.id },
        el('span', { class: 'nm' }, pg.name),
        pg.components.length ? el('span', { class: 'count' }, String(pg.components.length)) : null,
        el('button', { class: 'more', title: 'Page options', 'aria-label': `Options for ${pg.name}`, html: icon('more', 14), onclick: (ev) => { ev.stopPropagation(); pageMenu(pg.id, ev.currentTarget); } }));
      tab.addEventListener('click', () => switchPage(pg.id));
      tab.addEventListener('dblclick', (ev) => { ev.preventDefault(); startRename(pg.id); });
      tab.addEventListener('contextmenu', (ev) => { ev.preventDefault(); pageMenu(pg.id, { x: ev.clientX, y: ev.clientY }); });
      tab.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') switchPage(pg.id); if (ev.key === 'F2') startRename(pg.id); });
      bar.append(tab);
    });
    bar.append(el('button', { class: 'page-add', title: 'Add page', 'aria-label': 'Add page', html: icon('plus'), onclick: () => addPage() }));
  }
  function switchPage(id) {
    if (id === project.activePageId) return;
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    project.activePageId = id;
    selection.clear();
    hoverId = null;
    persist();
    renderPagebar();
    renderCanvas();
    renderInspector();
  }
  function addPage() {
    const cur = page();
    const names = new Set(project.pages.map((p) => p.name));
    let i = project.pages.length + 1;
    while (names.has('Page ' + i)) i++;
    const pg = newPage('Page ' + i, { w: cur.w, h: cur.h });
    project.pages.push(pg);
    project.activePageId = pg.id;
    selection.clear();
    commit();
    renderAll();
    startRename(pg.id);
  }
  function duplicatePage(id) {
    const src = project.pages.find((p) => p.id === id);
    const copy = clone(src);
    copy.id = 'p-' + uid();
    copy.name = src.name + ' copy';
    const gmap = {};
    copy.components.forEach((c) => { c.id = uid(); if (c.groupId) c.groupId = gmap[c.groupId] || (gmap[c.groupId] = 'g-' + uid()); });
    project.pages.splice(project.pages.indexOf(src) + 1, 0, copy);
    project.activePageId = copy.id;
    selection.clear();
    commit();
    renderAll();
    toast('Page duplicated');
  }
  async function deletePage(id) {
    if (project.pages.length === 1) return toast('A project needs at least one page');
    const pg = project.pages.find((p) => p.id === id);
    if (pg.components.length && !(await confirmDialog('Delete page?', `“${pg.name}” has ${pg.components.length} component(s). You can undo this with Ctrl+Z.`, 'Delete page', true))) return;
    const idx = project.pages.indexOf(pg);
    project.pages.splice(idx, 1);
    if (project.activePageId === id) project.activePageId = project.pages[Math.max(0, idx - 1)].id;
    selection.clear();
    commit();
    renderAll();
    toast('Page deleted');
  }
  function movePage(id, d) {
    const i = project.pages.findIndex((p) => p.id === id);
    const j = i + d;
    if (j < 0 || j >= project.pages.length) return;
    const [pg] = project.pages.splice(i, 1);
    project.pages.splice(j, 0, pg);
    commit();
    renderPagebar();
  }
  function startRename(id) {
    const tab = $(`.page-tab[data-page="${CSS.escape(id)}"]`);
    if (!tab) return;
    const pg = project.pages.find((p) => p.id === id);
    const nm = tab.querySelector('.nm');
    const inp = el('input', { value: pg.name, 'aria-label': 'Page name', spellcheck: 'false' });
    nm.replaceWith(inp);
    inp.focus();
    inp.select();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const v = inp.value.trim();
      if (save && v && v !== pg.name) { pg.name = v; commit(); }
      renderPagebar();
      renderStatus();
      if (!selected().length) renderInspector();
    };
    inp.addEventListener('keydown', (ev) => { ev.stopPropagation(); if (ev.key === 'Enter') finish(true); if (ev.key === 'Escape') finish(false); });
    inp.addEventListener('blur', () => finish(true));
    inp.addEventListener('click', (ev) => ev.stopPropagation());
    inp.addEventListener('dblclick', (ev) => ev.stopPropagation());
  }
  function pageMenu(id, anchor) {
    const i = project.pages.findIndex((p) => p.id === id);
    showMenu(anchor, [
      { label: 'Rename', icon: 'rename', kb: 'F2', action: () => { switchPage(id); startRename(id); } },
      { label: 'Duplicate', icon: 'copy', action: () => duplicatePage(id) },
      { label: 'Move left', icon: 'left', disabled: i === 0, action: () => movePage(id, -1) },
      { label: 'Move right', icon: 'right', disabled: i === project.pages.length - 1, action: () => movePage(id, 1) },
      '-',
      { label: 'Delete page', icon: 'trash', danger: true, disabled: project.pages.length === 1, action: () => deletePage(id) }
    ]);
  }

  /* ================================================================== menus & modals */
  function closeMenu() {
    const root = $('#menuRoot');
    if (root.firstChild) root.replaceChildren();
    document.removeEventListener('pointerdown', menuOutside, true);
  }
  function menuOutside(ev) {
    if (!ev.target.closest('.menu')) closeMenu();
  }
  function showMenu(anchor, items) {
    closeMenu();
    const m = el('div', { class: 'menu', role: 'menu' });
    items.forEach((it) => {
      if (it === '-') { m.append(el('hr')); return; }
      const b = el('button', { role: 'menuitem', class: it.danger ? 'danger' : '', disabled: !!it.disabled, html: (it.icon ? icon(it.icon) : '<span style="width:16px"></span>') + `<span>${esc(it.label)}</span>` + (it.kb ? `<span class="kb">${esc(it.kb)}</span>` : '') });
      b.addEventListener('click', () => { closeMenu(); it.action(); });
      m.append(b);
    });
    $('#menuRoot').append(m);
    let x, y;
    if (anchor && anchor.getBoundingClientRect) { const r = anchor.getBoundingClientRect(); x = r.left; y = r.bottom + 4; } else { x = anchor.x; y = anchor.y; }
    const mr = m.getBoundingClientRect();
    m.style.left = clamp(x, 4, window.innerWidth - mr.width - 4) + 'px';
    m.style.top = clamp(y, 4, window.innerHeight - mr.height - 4) + 'px';
    setTimeout(() => document.addEventListener('pointerdown', menuOutside, true), 0);
    const first = m.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  function openModal({ title, narrow, side, onClose }) {
    closeModal();
    const body = el('div', { class: 'modal-body' });
    const foot = el('div', { class: 'modal-foot' });
    const m = el('div', { class: 'modal' + (narrow ? ' narrow' : '') + (side ? ' side' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
      el('div', { class: 'modal-head' }, el('h2', {}, title), el('button', { class: 'btn icon', title: 'Close (Esc)', 'aria-label': 'Close', html: icon('close'), onclick: () => closeModal() })),
      body, foot);
    const back = el('div', { class: 'modal-backdrop' + (side ? ' side' : '') }, m);
    back.addEventListener('pointerdown', (ev) => { if (ev.target === back) closeModal(); });
    modalState = { back, onClose };
    $('#modalRoot').append(back);
    return { body, foot, modal: m };
  }
  function closeModal() {
    if (!modalState) return;
    const s = modalState;
    modalState = null;
    s.back.remove();
    if (s.onClose) s.onClose();
  }
  function confirmDialog(title, text, okLabel = 'OK', danger = false) {
    return new Promise((resolve) => {
      let result = false;
      const { body, foot } = openModal({ title, narrow: true, onClose: () => resolve(result) });
      body.append(el('p', { style: { margin: '0', color: 'var(--ui-text-2)' } }, text));
      const ok = el('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => { result = true; closeModal(); } }, okLabel);
      foot.append(el('span', { class: 'grow' }), el('button', { class: 'btn', onclick: () => closeModal() }, 'Cancel'), ok);
      ok.focus();
    });
  }

  /* ================================================================== palette */
  function renderPalette() {
    const root = $('#palette');
    const search = el('input', { class: 'pal-search', type: 'search', placeholder: 'Find a component…', 'aria-label': 'Find a component' });
    const sections = el('div');
    const build = () => {
      const q = search.value.trim().toLowerCase();
      sections.replaceChildren();
      PALETTE.forEach((sec) => {
        const items = sec.items.filter((it) => !q || (it.name + ' ' + TYPES[it.type].label).toLowerCase().includes(q));
        if (!items.length) return;
        const grid = el('div', { class: 'pal-grid' });
        items.forEach((spec) => {
          const b = el('button', { class: 'pal-item', title: `Add ${spec.name} (click, or drag onto the page)`, html: icon(spec.icon || TYPES[spec.type].icon, 18) + `<span>${esc(spec.name)}</span>` });
          b.addEventListener('pointerdown', (ev) => paletteDown(ev, spec));
          b.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); addComponent(spec); } });
          grid.append(b);
        });
        sections.append(el('div', { class: 'pal-section' }, el('div', { class: 'pal-title' }, sec.title), grid));
      });
      if (!sections.firstChild) sections.append(el('p', { class: 'hint', style: { margin: '12px 2px' } }, 'No component matches.'));
    };
    search.addEventListener('input', build);
    search.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { search.value = ''; build(); search.blur(); } });
    build();
    root.replaceChildren(search, sections, el('p', { class: 'hint', style: { margin: '16px 2px 0' } }, 'Click to add in the middle of the view, or drag onto the page. Press ', el('kbd', {}, '?'), ' for shortcuts.'));
  }
  function paletteDown(ev, spec) {
    if (ev.button !== 0) return;
    ev.preventDefault();
    const T = TYPES[spec.type];
    const w = spec.w || T.w;
    const h = spec.h || T.h;
    const start = { x: ev.clientX, y: ev.clientY };
    let ghost = null;
    const move = (e) => {
      if (!ghost && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) {
        ghost = el('div', { class: 'pal-ghost' }, spec.name);
        document.body.append(ghost);
      }
      if (ghost) Object.assign(ghost.style, { width: w * zoom + 'px', height: Math.max(18, h * zoom) + 'px', left: e.clientX - (w * zoom) / 2 + 'px', top: e.clientY - (h * zoom) / 2 + 'px' });
    };
    const up = (e) => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      if (!ghost) { addComponent(spec); return; }
      ghost.remove();
      const vr = viewport.getBoundingClientRect();
      if (e.clientX < vr.left || e.clientX > vr.right || e.clientY < vr.top || e.clientY > vr.bottom) return;
      const p = toPage(e);
      addComponent(spec, p.x - w / 2, p.y - h / 2);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }
  function makeComp(spec, x, y) {
    const T = TYPES[spec.type];
    return {
      id: uid(), type: spec.type, name: uniqueName(spec.name || T.label), label: spec.label != null ? spec.label : T.defLabel,
      x, y, w: spec.w || T.w, h: spec.h || T.h,
      options: Object.assign(clone(T.options), clone(spec.options || {})), notes: '', styleSetId: null, overrides: {}, groupId: null
    };
  }
  function addComponent(spec, x, y) {
    const pg = page();
    const T = TYPES[spec.type];
    const w = spec.w || T.w;
    const h = spec.h || T.h;
    if (x == null) {
      const vr = viewport.getBoundingClientRect();
      const p = toPage({ clientX: vr.left + vr.width / 2, clientY: vr.top + vr.height / 2 });
      x = p.x - w / 2;
      y = p.y - h / 2;
      x = clamp(snapV(x), 0, Math.max(0, pg.w - w));
      y = clamp(snapV(y), 0, Math.max(0, pg.h - h));
      while (pg.components.some((c) => c.x === x && c.y === y)) { x += 16; y += 16; }
    } else if (ui.snap) { x = snapV(x); y = snapV(y); } else { x = Math.round(x); y = Math.round(y); }
    const c = makeComp(spec, x, y);
    pg.components.push(c);
    selection = new Set([c.id]);
    commit();
    renderCanvas();
    renderInspector();
    toast(`Added ${c.name}`);
  }

  /* ================================================================== edit operations */
  function deleteSelection() {
    if (!selection.size) return;
    const n = selection.size;
    page().components = comps().filter((c) => !selection.has(c.id));
    selection.clear();
    commit();
    refresh();
    renderInspector();
    toast(n === 1 ? 'Deleted' : `Deleted ${n} items`);
  }
  function copySelection(cut) {
    const sel = selected();
    if (!sel.length) return;
    clipboard = { items: clone(sel), pageId: page().id };
    if (cut) deleteSelection(); else toast(sel.length === 1 ? 'Copied' : `Copied ${sel.length} items`);
  }
  function pasteItems(items, offset) {
    const pg = page();
    const gmap = {};
    const added = items.map((src) => {
      const c = clone(src);
      c.id = uid();
      c.x += offset;
      c.y += offset;
      if (c.groupId) c.groupId = gmap[c.groupId] || (gmap[c.groupId] = 'g-' + uid());
      return c;
    });
    added.forEach((c) => { c.name = uniqueName(c.name.replace(/ \d+$/, '')); pg.components.push(c); });
    selection = new Set(added.map((c) => c.id));
    commit();
    refresh();
    renderInspector();
  }
  function paste() {
    if (!clipboard) return toast('Clipboard is empty');
    const pg = page();
    const collides = clipboard.items.some((it) => pg.components.some((c) => c.x === it.x && c.y === it.y && c.w === it.w && c.h === it.h));
    pasteItems(clipboard.items, collides ? 16 : 0);
    toast('Pasted');
  }
  function duplicateSelection() {
    const sel = selected();
    if (!sel.length) return;
    pasteItems(clone(sel), 16);
    toast('Duplicated');
  }
  function nudge(dx, dy) {
    const sel = selected();
    if (!sel.length) return;
    sel.forEach((c) => { c.x += dx; c.y += dy; });
    commit('nudge');
    refresh();
    updateGeoFields();
  }
  function reorder(kind) {
    const pg = page();
    const list = pg.components;
    if (!selection.size) return;
    const isSel = (c) => selection.has(c.id);
    if (kind === 'front') pg.components = [...list.filter((c) => !isSel(c)), ...list.filter(isSel)];
    else if (kind === 'back') pg.components = [...list.filter(isSel), ...list.filter((c) => !isSel(c))];
    else if (kind === 'forward') {
      for (let i = list.length - 2; i >= 0; i--) if (isSel(list[i]) && !isSel(list[i + 1])) [list[i], list[i + 1]] = [list[i + 1], list[i]];
    } else if (kind === 'backward') {
      for (let i = 1; i < list.length; i++) if (isSel(list[i]) && !isSel(list[i - 1])) [list[i], list[i - 1]] = [list[i - 1], list[i]];
    }
    commit();
    refresh();
    renderInspector();
    toast({ front: 'Brought to front', back: 'Sent to back', forward: 'Brought forward', backward: 'Sent backward' }[kind]);
  }
  function groupSelection() {
    const sel = selected();
    if (sel.length < 2) return toast('Select two or more items to group');
    const gid = 'g-' + uid();
    sel.forEach((c) => { c.groupId = gid; });
    commit();
    refresh();
    renderInspector();
    toast('Grouped');
  }
  function ungroupSelection() {
    const sel = selected().filter((c) => c.groupId);
    if (!sel.length) return toast('Nothing grouped in the selection');
    sel.forEach((c) => { c.groupId = null; });
    commit();
    refresh();
    renderInspector();
    toast('Ungrouped');
  }
  function align(kind) {
    const sel = selected();
    if (sel.length < 2) return;
    const bb = bbox(sel);
    sel.forEach((c) => {
      if (kind === 'l') c.x = bb.x;
      if (kind === 'ch') c.x = Math.round(bb.x + (bb.w - c.w) / 2);
      if (kind === 'r') c.x = bb.x + bb.w - c.w;
      if (kind === 't') c.y = bb.y;
      if (kind === 'cv') c.y = Math.round(bb.y + (bb.h - c.h) / 2);
      if (kind === 'b') c.y = bb.y + bb.h - c.h;
    });
    commit();
    refresh();
  }
  function selectAll() {
    selection = new Set(comps().map((c) => c.id));
    renderOverlay();
    renderInspector();
    renderStatus();
  }
  function clearSelection() {
    if (!selection.size) return;
    selection.clear();
    renderOverlay();
    renderInspector();
    renderStatus();
  }

  /* ================================================================== zoom & pan */
  function toPage(ev) {
    const r = pageWrap.getBoundingClientRect();
    return { x: (ev.clientX - r.left) / zoom, y: (ev.clientY - r.top) / zoom };
  }
  function setZoom(z, anchor) {
    z = clamp(Math.round(z * 100) / 100, ZOOM_MIN, ZOOM_MAX);
    if (z === zoom) { renderToolbar(); return; }
    const vr = viewport.getBoundingClientRect();
    const a = anchor || { clientX: vr.left + vr.width / 2, clientY: vr.top + vr.height / 2 };
    const p = toPage(a);
    zoom = z;
    renderCanvas();
    renderToolbar();
    const r = pageWrap.getBoundingClientRect();
    viewport.scrollLeft += r.left + p.x * zoom - a.clientX;
    viewport.scrollTop += r.top + p.y * zoom - a.clientY;
    drawRulers();
  }
  function zoomStep(dir) {
    const cur = zoom;
    const next = dir > 0 ? ZOOM_STEPS.find((s) => s > cur + 0.001) : [...ZOOM_STEPS].reverse().find((s) => s < cur - 0.001);
    setZoom(next || cur);
  }
  function zoomFit() {
    const pg = page();
    const availW = viewport.clientWidth - 112;
    const availH = viewport.clientHeight - 112;
    zoom = clamp(Math.floor(Math.min(availW / pg.w, availH / pg.h) * 100) / 100, ZOOM_MIN, ZOOM_MAX);
    renderCanvas();
    renderToolbar();
    viewport.scrollLeft = (viewport.scrollWidth - viewport.clientWidth) / 2;
    viewport.scrollTop = (viewport.scrollHeight - viewport.clientHeight) / 2;
    drawRulers();
  }

  /* ================================================================== snapping */
  function snapTargets(ignore) {
    const pg = page();
    const xs = [0, pg.w / 2, pg.w];
    const ys = [0, pg.h / 2, pg.h];
    for (const c of pg.components) {
      if (ignore.has(c.id)) continue;
      xs.push(c.x, c.x + c.w / 2, c.x + c.w);
      ys.push(c.y, c.y + c.h / 2, c.y + c.h);
    }
    return { xs, ys };
  }
  function bestSnap(points, targets, th) {
    let best = null;
    for (const p of points) for (const t of targets) {
      const d = t - p;
      if (Math.abs(d) <= th && (!best || Math.abs(d) < Math.abs(best.delta))) best = { delta: d, target: t };
    }
    return best;
  }
  function snapMove(bb, dx, dy, ignore, off) {
    if (!ui.snap || off) return { dx: Math.round(dx), dy: Math.round(dy), guides: [] };
    const th = 5 / zoom;
    const { xs, ys } = snapTargets(ignore);
    const guides = [];
    let nx = bb.x + dx;
    let ny = bb.y + dy;
    const sx = bestSnap([nx, nx + bb.w / 2, nx + bb.w], xs, th);
    if (sx) { nx += sx.delta; guides.push({ axis: 'x', pos: sx.target }); } else nx = snapV(nx);
    const sy = bestSnap([ny, ny + bb.h / 2, ny + bb.h], ys, th);
    if (sy) { ny += sy.delta; guides.push({ axis: 'y', pos: sy.target }); } else ny = snapV(ny);
    return { dx: Math.round(nx - bb.x), dy: Math.round(ny - bb.y), guides };
  }
  function snapEdge(v, targets, axis, guides, off) {
    if (!ui.snap || off) return Math.round(v);
    const s = bestSnap([v], targets, 5 / zoom);
    if (s) { guides.push({ axis, pos: s.target }); return s.target; }
    return snapV(v);
  }
  function resizeBox(b, dir, dx, dy, keep, off, ignore) {
    const guides = [];
    const { xs, ys } = snapTargets(ignore);
    let x1 = b.x, y1 = b.y, x2 = b.x + b.w, y2 = b.y + b.h;
    if (dir.includes('w')) x1 = snapEdge(b.x + dx, xs, 'x', guides, off);
    if (dir.includes('e')) x2 = snapEdge(b.x + b.w + dx, xs, 'x', guides, off);
    if (dir.includes('n')) y1 = snapEdge(b.y + dy, ys, 'y', guides, off);
    if (dir.includes('s')) y2 = snapEdge(b.y + b.h + dy, ys, 'y', guides, off);
    const MIN = 4;
    if (x2 - x1 < MIN) { if (dir.includes('w')) x1 = x2 - MIN; else x2 = x1 + MIN; }
    if (y2 - y1 < MIN) { if (dir.includes('n')) y1 = y2 - MIN; else y2 = y1 + MIN; }
    let w = x2 - x1;
    let h = y2 - y1;
    if (keep && b.w > 0 && b.h > 0) {
      const ratio = b.w / b.h;
      const corner = dir.length === 2;
      if (corner) { if (w / h > ratio) w = h * ratio; else h = w / ratio; }
      else if (dir === 'e' || dir === 'w') h = w / ratio;
      else w = h * ratio;
      w = Math.max(MIN, w);
      h = Math.max(MIN, h);
      x1 = dir.includes('w') ? b.x + b.w - w : (corner || dir === 'e' || dir === 'w' ? b.x : b.x + (b.w - w) / 2);
      y1 = dir.includes('n') ? b.y + b.h - h : (corner || dir === 'n' || dir === 's' ? b.y : b.y + (b.h - h) / 2);
      guides.length = 0;
    }
    return { x: Math.round(x1), y: Math.round(y1), w: Math.round(w), h: Math.round(h), guides };
  }

  /* ================================================================== pointer interaction */
  function blurInputs() {
    const a = document.activeElement;
    if (a && a !== document.body && /INPUT|TEXTAREA|SELECT/.test(a.tagName)) a.blur();
  }
  viewport.addEventListener('pointerdown', (ev) => {
    closeMenu();
    if (ev.button === 1 || (ev.button === 0 && spaceHeld)) {
      ev.preventDefault();
      drag = { mode: 'pan', sx: ev.clientX, sy: ev.clientY, sl: viewport.scrollLeft, st: viewport.scrollTop };
      viewport.classList.add('panning');
      return;
    }
    if (ev.button !== 0) return;
    if (ev.target === viewport && (ev.offsetX > viewport.clientWidth || ev.offsetY > viewport.clientHeight)) return; // scrollbar
    blurInputs();
    viewport.focus({ preventScroll: true });
    const p = toPage(ev);
    const handle = ev.target.closest('.handle');
    if (handle && selection.size) {
      ev.preventDefault();
      const sel = selected();
      drag = { mode: 'resize', dir: handle.dataset.dir, start: p, bb: bbox(sel), orig: new Map(sel.map((c) => [c.id, { x: c.x, y: c.y, w: c.w, h: c.h }])), ids: new Set(sel.map((c) => c.id)), moved: false };
      return;
    }
    const node = ev.target.closest('.cmp');
    if (node && pageEl.contains(node)) {
      ev.preventDefault();
      const id = node.dataset.id;
      const ids = expandGroups([id]);
      if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
        const has = selection.has(id);
        ids.forEach((i) => (has ? selection.delete(i) : selection.add(i)));
        renderOverlay();
        renderInspector();
        renderStatus();
        drawRulers();
        if (has) return;
      } else if (!selection.has(id)) {
        selection = ids;
        renderOverlay();
        renderInspector();
        renderStatus();
        drawRulers();
      }
      const sel = selected();
      drag = { mode: 'move', start: p, bb: bbox(sel), orig: new Map(sel.map((c) => [c.id, { x: c.x, y: c.y }])), ids: new Set(sel.map((c) => c.id)), moved: false };
      return;
    }
    ev.preventDefault();
    const additive = ev.shiftKey || ev.ctrlKey || ev.metaKey;
    if (!additive && selection.size) { selection.clear(); renderOverlay(); renderInspector(); renderStatus(); drawRulers(); }
    drag = { mode: 'marquee', start: p, base: new Set(selection), rect: null };
  });

  window.addEventListener('pointermove', (ev) => {
    if (!drag) {
      if (ev.target instanceof Node && viewport.contains(ev.target)) {
        const node = ev.target.closest('.cmp');
        const id = node && pageEl.contains(node) ? node.dataset.id : null;
        if (id !== hoverId) { hoverId = id; renderOverlay(); }
        const p = toPage(ev);
        const pg = page();
        cursor = p.x >= 0 && p.y >= 0 && p.x <= pg.w && p.y <= pg.h ? p : null;
        renderStatusSoon();
      }
      return;
    }
    if (drag.mode === 'pan') {
      viewport.scrollLeft = drag.sl - (ev.clientX - drag.sx);
      viewport.scrollTop = drag.st - (ev.clientY - drag.sy);
      drawRulers();
      return;
    }
    const p = toPage(ev);
    let dx = p.x - drag.start.x;
    let dy = p.y - drag.start.y;
    if (!drag.moved) {
      if (Math.hypot(dx, dy) * zoom < 3) return;
      drag.moved = true;
      hoverId = null;
    }
    if (drag.mode === 'move') {
      if (ev.shiftKey) { if (Math.abs(dx) > Math.abs(dy)) dy = 0; else dx = 0; }
      const s = snapMove(drag.bb, dx, dy, drag.ids, ev.altKey);
      if (ev.shiftKey) { if (dx === 0) s.dx = 0; if (dy === 0) s.dy = 0; }
      for (const c of selected()) {
        const o = drag.orig.get(c.id);
        if (!o) continue;
        c.x = o.x + s.dx;
        c.y = o.y + s.dy;
        const node = pageEl.querySelector(`[data-id="${CSS.escape(c.id)}"]`);
        if (node) { node.style.left = c.x + 'px'; node.style.top = c.y + 'px'; }
      }
      renderOverlay(s.guides);
      updateGeoFields();
      renderStatusSoon();
      drawRulers();
    } else if (drag.mode === 'resize') {
      const sel = selected();
      const keep = ev.shiftKey || (sel.length === 1 && sel[0].type === 'media');
      const r = resizeBox(drag.bb, drag.dir, dx, dy, keep, ev.altKey, drag.ids);
      const sx = r.w / drag.bb.w;
      const sy = r.h / drag.bb.h;
      for (const c of sel) {
        const o = drag.orig.get(c.id);
        if (!o) continue;
        if (sel.length === 1) Object.assign(c, { x: r.x, y: r.y, w: r.w, h: r.h });
        else {
          c.x = Math.round(r.x + (o.x - drag.bb.x) * sx);
          c.y = Math.round(r.y + (o.y - drag.bb.y) * sy);
          c.w = Math.max(1, Math.round(o.w * sx));
          c.h = Math.max(1, Math.round(o.h * sy));
        }
        renderOne(c);
      }
      renderOverlay(r.guides);
      updateGeoFields();
      renderStatusSoon();
      drawRulers();
    } else if (drag.mode === 'marquee') {
      const r = { x: Math.min(p.x, drag.start.x), y: Math.min(p.y, drag.start.y), w: Math.abs(dx), h: Math.abs(dy) };
      drag.rect = r;
      const hits = comps().filter((c) => c.x < r.x + r.w && c.x + c.w > r.x && c.y < r.y + r.h && c.y + c.h > r.y).map((c) => c.id);
      selection = expandGroups([...drag.base, ...hits]);
      renderOverlay();
    }
  });

  window.addEventListener('pointerup', () => {
    if (!drag) return;
    const d = drag;
    drag = null;
    viewport.classList.remove('panning');
    if (d.mode === 'pan') return;
    if ((d.mode === 'move' || d.mode === 'resize') && d.moved) {
      commit();
      if (d.mode === 'resize') refresh(); else { renderOverlay(); renderStatus(); }
      updateGeoFields();
      return;
    }
    renderOverlay();
    if (d.mode === 'marquee') { renderInspector(); renderStatus(); drawRulers(); }
  });
  window.addEventListener('blur', () => { spaceHeld = false; viewport.classList.remove('space'); });

  viewport.addEventListener('dblclick', (ev) => {
    const node = ev.target.closest('.cmp');
    if (!node) return;
    selection = new Set([node.dataset.id]);
    renderOverlay();
    renderInspector();
    const f = $('#insp-label') || $('#insp-name');
    if (f) { f.focus(); f.select(); }
  });
  viewport.addEventListener('contextmenu', (ev) => {
    ev.preventDefault();
    const node = ev.target.closest('.cmp');
    if (node && !selection.has(node.dataset.id)) { selection = expandGroups([node.dataset.id]); renderOverlay(); renderInspector(); renderStatus(); }
    const has = selection.size > 0;
    showMenu({ x: ev.clientX, y: ev.clientY }, [
      { label: 'Duplicate', icon: 'copy', kb: 'Ctrl+D', disabled: !has, action: duplicateSelection },
      { label: 'Copy', icon: 'copy', kb: 'Ctrl+C', disabled: !has, action: () => copySelection(false) },
      { label: 'Paste', icon: 'paste', kb: 'Ctrl+V', disabled: !clipboard, action: paste },
      '-',
      { label: 'Bring forward', icon: 'front', kb: 'Ctrl+]', disabled: !has, action: () => reorder('forward') },
      { label: 'Send backward', icon: 'back', kb: 'Ctrl+[', disabled: !has, action: () => reorder('backward') },
      { label: 'Group', icon: 'group', kb: 'Ctrl+G', disabled: selection.size < 2, action: groupSelection },
      { label: 'Ungroup', icon: 'ungroup', kb: 'Ctrl+Shift+G', disabled: !selected().some((c) => c.groupId), action: ungroupSelection },
      '-',
      { label: 'Delete', icon: 'trash', kb: 'Del', danger: true, disabled: !has, action: deleteSelection }
    ]);
  });
  viewport.addEventListener('wheel', (ev) => {
    if (!(ev.ctrlKey || ev.metaKey)) return;
    ev.preventDefault();
    setZoom(zoom * Math.exp(-ev.deltaY * 0.0022), ev);
  }, { passive: false });
  viewport.addEventListener('scroll', () => drawRulers());
  viewport.addEventListener('pointerleave', () => { if (hoverId && !drag) { hoverId = null; renderOverlay(); } cursor = null; renderStatusSoon(); });
  window.addEventListener('resize', () => drawRulers());

  let statusQueued = false;
  function renderStatusSoon() {
    if (statusQueued) return;
    statusQueued = true;
    requestAnimationFrame(() => { statusQueued = false; renderStatus(); });
  }

  /* ================================================================== keyboard */
  document.addEventListener('keydown', (ev) => {
    const t = ev.target;
    const editing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
    const mod = ev.ctrlKey || ev.metaKey;
    const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    if (modalState) {
      if (ev.key === 'Escape') { ev.preventDefault(); closeModal(); }
      return;
    }
    if ($('#menuRoot').firstChild && ev.key === 'Escape') { closeMenu(); return; }
    if (mod && k === 's') { ev.preventDefault(); if (editing) t.blur(); saveProjectFile(); return; }
    if (mod && k === 'e') { ev.preventDefault(); if (editing) t.blur(); openExport(); return; }
    if (editing) {
      if (ev.key === 'Escape') t.blur();
      return;
    }
    if (ev.key === ' ') {
      ev.preventDefault();
      if (!spaceHeld) { spaceHeld = true; viewport.classList.add('space'); }
      return;
    }
    if (mod && k === 'z' && !ev.shiftKey) { ev.preventDefault(); undo(); return; }
    if (mod && (k === 'y' || (k === 'z' && ev.shiftKey))) { ev.preventDefault(); redo(); return; }
    if (mod && k === 'd') { ev.preventDefault(); duplicateSelection(); return; }
    if (mod && k === 'c') { ev.preventDefault(); copySelection(false); return; }
    if (mod && k === 'x') { ev.preventDefault(); copySelection(true); return; }
    if (mod && k === 'v') { ev.preventDefault(); paste(); return; }
    if (mod && k === 'a') { ev.preventDefault(); selectAll(); return; }
    if (mod && k === 'g') { ev.preventDefault(); if (ev.shiftKey) ungroupSelection(); else groupSelection(); return; }
    if (mod && (ev.code === 'BracketRight' || ev.code === 'BracketLeft')) {
      ev.preventDefault();
      const fwd = ev.code === 'BracketRight';
      reorder(ev.shiftKey ? (fwd ? 'front' : 'back') : (fwd ? 'forward' : 'backward'));
      return;
    }
    if (mod && (k === '=' || k === '+')) { ev.preventDefault(); zoomStep(1); return; }
    if (mod && (k === '-' || k === '_')) { ev.preventDefault(); zoomStep(-1); return; }
    if (mod && k === '0') { ev.preventDefault(); setZoom(1); return; }
    if (mod) return;
    if (ev.shiftKey && ev.code === 'Digit1') { ev.preventDefault(); zoomFit(); return; }
    if (ev.key === '?') { ev.preventDefault(); openShortcuts(); return; }
    if (ev.key === 'Delete' || ev.key === 'Backspace') { ev.preventDefault(); deleteSelection(); return; }
    if (ev.key === 'Escape') { clearSelection(); return; }
    if (ev.key === 'Enter' && selection.size === 1) { ev.preventDefault(); const f = $('#insp-label') || $('#insp-name'); if (f) { f.focus(); f.select(); } return; }
    const arrows = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (arrows[ev.key] && selection.size) {
      ev.preventDefault();
      const s = ev.shiftKey ? GRID : 1;
      nudge(arrows[ev.key][0] * s, arrows[ev.key][1] * s);
    }
  });
  document.addEventListener('keyup', (ev) => {
    if (ev.key === ' ') { spaceHeld = false; viewport.classList.remove('space'); }
  });

  /* ================================================================== inspector */
  function field(label, control, opts = {}) {
    return el('div', { class: 'field' }, el('label', { for: control.id || null }, label), control, opts.hint ? el('div', { class: 'hint' }, opts.hint) : null);
  }
  function section(title, ...children) {
    return el('div', { class: 'insp-section' }, title ? el('div', { class: 'insp-title' }, title) : null, ...children);
  }
  function textCtl(value, onInput, onChange, opts = {}) {
    const i = opts.multiline
      ? el('textarea', { class: 'txt', rows: opts.rows || 3, placeholder: opts.placeholder || '', id: opts.id || null, spellcheck: 'true' })
      : el('input', { class: 'inp', type: opts.type || 'text', placeholder: opts.placeholder || '', id: opts.id || null, min: opts.min, max: opts.max, step: opts.step, spellcheck: 'false' });
    i.value = value == null ? '' : value;
    i.addEventListener('input', () => onInput(i.value));
    i.addEventListener('change', () => onChange(i.value));
    if (!opts.multiline) i.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') i.blur(); });
    return i;
  }
  function selectCtl(value, options, onChange, id) {
    const s = el('select', { class: 'sel', id: id || null }, options.map(([v, l]) => el('option', { value: v }, l)));
    s.value = value == null ? '' : String(value);
    s.addEventListener('change', () => onChange(s.value));
    return s;
  }
  function checkCtl(label, value, onChange) {
    const i = el('input', { type: 'checkbox' });
    i.checked = !!value;
    i.addEventListener('change', () => onChange(i.checked));
    return el('label', { class: 'check' }, i, label);
  }
  function colorCtl(value, onInput, onChange) {
    const text = el('input', { class: 'inp', placeholder: 'Style default', spellcheck: 'false' });
    text.value = value || '';
    const pick = el('input', { type: 'color', title: 'Pick a colour' });
    pick.value = toHex(value) || '#ffffff';
    text.addEventListener('input', () => { const hx = toHex(text.value); if (hx) pick.value = hx; onInput(text.value.trim()); });
    text.addEventListener('change', () => onChange(text.value.trim()));
    text.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') text.blur(); });
    pick.addEventListener('input', () => { text.value = pick.value; onInput(pick.value); });
    pick.addEventListener('change', () => onChange(pick.value));
    return el('div', { class: 'color-field' }, pick, text);
  }
  const hexCtx = document.createElement('canvas').getContext('2d');
  function toHex(v) {
    if (!v) return null;
    if (/^#[0-9a-f]{6}$/i.test(v)) return v.toLowerCase();
    if (/^#[0-9a-f]{3}$/i.test(v)) return '#' + v.slice(1).split('').map((c) => c + c).join('').toLowerCase();
    hexCtx.fillStyle = '#010203';
    hexCtx.fillStyle = v;
    const out = hexCtx.fillStyle;
    if (out === '#010203' && v.trim().toLowerCase() !== '#010203') return null;
    if (/^#[0-9a-f]{6}$/i.test(out)) return out;
    const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(out);
    return m ? '#' + [m[1], m[2], m[3]].map((n) => (+n).toString(16).padStart(2, '0')).join('') : null;
  }

  function styleOptions(inheritLabel) {
    return [['', inheritLabel], ...allSets().map((s) => [s.id, s.name])];
  }

  function renderInspector() {
    const root = $('#inspector');
    const sel = selected();
    let content;
    if (!sel.length) content = pagePanel();
    else if (sel.length === 1) content = compPanel(sel[0]);
    else content = multiPanel(sel);
    root.replaceChildren(...content);
  }
  function updateGeoFields() {
    const sel = selected();
    if (sel.length !== 1) return;
    const c = sel[0];
    document.querySelectorAll('[data-geo]').forEach((i) => { if (document.activeElement !== i) i.value = c[i.dataset.geo]; });
  }

  function pagePanel() {
    const pg = page();
    const set = currentSet();
    const sizeVal = PAGE_SIZES.find((s) => s.w === pg.w && s.h === pg.h);
    const head = el('div', { class: 'insp-head' }, el('div', { class: 'type-icon', html: icon('page') }), el('div', {}, el('div', { class: 'ttl' }, pg.name), el('div', { class: 'sub' }, 'Page · nothing selected')));
    const pageSec = section('Page',
      field('Page name', textCtl(pg.name, (v) => { pg.name = v; renderPagebarSoon(); }, () => { if (!pg.name.trim()) pg.name = 'Page'; commit(); renderPagebar(); }, { id: 'insp-page-name' })),
      field('Screen size', selectCtl(sizeVal ? `${pg.w}x${pg.h}` : 'custom', [...PAGE_SIZES.map((s) => [`${s.w}x${s.h}`, s.label]), ['custom', 'Custom…']], (v) => {
        const [w, h] = String(v).split('x').map(Number);
        if (w > 0 && h > 0) { pg.w = w; pg.h = h; commit(); zoomFit(); }
        renderInspector();
      })),
      el('div', { class: 'geo' },
        geoNum('W', pg.w, (v) => { pg.w = clamp(Math.round(v), 100, 8000); refresh(); }),
        geoNum('H', pg.h, (v) => { pg.h = clamp(Math.round(v), 100, 8000); refresh(); })),
      el('div', { style: { height: '10px' } }),
      field('Page style', selectCtl(pg.styleSetId || '', styleOptions(`Project style (${getSet(project.styleSetId).name})`), (v) => { pg.styleSetId = v || null; commit(); refresh(); renderInspector(); })),
      el('div', { class: 'field-inline' }, checkCtl('Page tint (whole project)', project.tint, (v) => { project.tint = v; commit(); refresh(); })),
      field('What this screen is for', textCtl(pg.notes, (v) => { pg.notes = v; }, () => commit(), { multiline: true, rows: 3, placeholder: 'e.g. Home screen: what is rendering tonight and what needs my approval.' })));
    const tokSec = section('Style set',
      el('div', { class: 'hint', style: { marginBottom: '8px' } }, `${set.name}: ${set.description || 'custom set'}`),
      el('div', { class: 'row-actions' }, el('button', { class: 'btn small', html: icon('sliders', 14) + '<span>Edit tokens…</span>', onclick: openTokenEditor })));
    const layers = el('div', { class: 'layers' });
    [...pg.components].reverse().forEach((c) => {
      layers.append(el('button', { class: 'layer', title: `${c.name} (${TYPES[c.type].label})`, html: icon(paletteIcon(c), 14) + `<span class="nm">${esc(c.name)}</span><span class="tp">${esc(TYPES[c.type].label)}</span>`, onclick: () => { selection = expandGroups([c.id]); renderOverlay(); renderInspector(); renderStatus(); } }));
    });
    const layerSec = section(`Layers (${pg.components.length})`, pg.components.length ? layers : el('div', { class: 'hint' }, 'No components yet. Top of the list is the front-most item.'));
    return [head, pageSec, tokSec, layerSec];
  }
  let pagebarQueued = false;
  function renderPagebarSoon() {
    if (pagebarQueued) return;
    pagebarQueued = true;
    requestAnimationFrame(() => { pagebarQueued = false; renderPagebar(); renderStatus(); });
  }
  function paletteIcon(c) {
    if (c.type === 'text') return { heading: 'heading', body: 'body', caption: 'caption' }[c.options.variant] || 'body';
    if (c.type === 'media' && c.options.ratio === '9:16') return 'mediaV';
    if (c.type === 'chart' && c.options.chartType === 'bars') return 'chartBars';
    if (c.type === 'button') return { secondary: 'buttonOutline', ghost: 'buttonGhost' }[c.options.variant] || 'button';
    return TYPES[c.type].icon;
  }
  function geoNum(lbl, value, onInput, key) {
    const i = el('input', { class: 'inp', type: 'number', step: 1, 'aria-label': lbl, 'data-geo': key || null });
    i.value = value;
    i.addEventListener('input', () => { if (i.value !== '') onInput(num(i.value)); });
    i.addEventListener('change', () => { commit(); updateGeoFields(); });
    i.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') i.blur(); });
    return el('div', { class: 'num' }, el('span', {}, lbl), i);
  }

  function compPanel(c) {
    const T = TYPES[c.type];
    const live = () => { renderOne(c); renderOverlay(); renderStatusSoon(); };
    const done = (rebuild) => { commit(); if (rebuild) renderInspector(); };
    const setOpt = (k, v, rebuild) => { c.options[k] = v; live(); done(rebuild); };
    const head = el('div', { class: 'insp-head' },
      el('div', { class: 'type-icon', html: icon(paletteIcon(c)) }),
      el('div', { style: { minWidth: '0' } }, el('div', { class: 'ttl ell', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, c.name), el('div', { class: 'sub' }, T.label + (c.groupId ? ' · in a group' : ''))));
    const labelIsText = c.type === 'text';
    const hasLabel = !['divider', 'icon', 'progress', 'tabs', 'navrail'].includes(c.type);
    const ident = section('Identity',
      field('Name (what is this?)', textCtl(c.name, (v) => { c.name = v; renderOverlay(); renderStatusSoon(); }, () => { if (!c.name.trim()) c.name = T.label; done(true); }, { id: 'insp-name', placeholder: 'e.g. Approve button' })),
      hasLabel ? field(labelIsText ? 'Text' : c.type === 'input' ? 'Placeholder' : c.type === 'card' || c.type === 'chart' ? 'Title' : 'Label', textCtl(c.label, (v) => { c.label = v; live(); }, () => done(), { id: 'insp-label', multiline: labelIsText, rows: 3 })) : null);
    const geo = section('Position & size',
      el('div', { class: 'geo' },
        geoNum('X', c.x, (v) => { c.x = Math.round(v); live(); }, 'x'),
        geoNum('Y', c.y, (v) => { c.y = Math.round(v); live(); }, 'y'),
        geoNum('W', c.w, (v) => { c.w = Math.max(1, Math.round(v)); live(); }, 'w'),
        geoNum('H', c.h, (v) => { c.h = Math.max(1, Math.round(v)); live(); }, 'h')));
    const opts = optionFields(c, setOpt, live, done);
    const optSec = opts.length ? section('Options', ...opts) : null;
    const notes = section('Notes',
      field('What it does', textCtl(c.notes, (v) => { c.notes = v; }, () => done(), { multiline: true, rows: 4, placeholder: 'e.g. Approves the selected render and moves it to Results.' })));
    const ov = c.overrides;
    const setOv = (k, v) => { if (v === '' || v == null) delete ov[k]; else ov[k] = v; live(); };
    const ovFields = OVERRIDES.map((o) => {
      if (o.kind === 'color') return field(o.label, colorCtl(ov[o.key], (v) => setOv(o.key, v), () => done()));
      if (o.kind === 'number') return field(o.label, textCtl(ov[o.key] ?? '', (v) => setOv(o.key, v === '' ? '' : num(v)), () => done(), { type: 'number', placeholder: 'Style default', min: 0 }));
      return field(o.label, selectCtl(ov[o.key] || '', o.options, (v) => { setOv(o.key, v); done(); }));
    });
    const styleSec = section('Style',
      field('Style set for this component', selectCtl(c.styleSetId || '', styleOptions(`Page style (${currentSet().name})`), (v) => { c.styleSetId = v || null; live(); done(); })),
      el('div', { class: 'insp-title', style: { margin: '12px 2px 8px' } }, 'Overrides'),
      ...ovFields,
      el('div', { class: 'row-actions' }, el('button', { class: 'btn small', disabled: !Object.keys(ov).length, onclick: () => { c.overrides = {}; live(); done(true); } }, 'Reset overrides')));
    const actions = section('Arrange',
      el('div', { class: 'row-actions' },
        el('button', { class: 'btn small', title: 'Duplicate (Ctrl+D)', html: icon('copy', 14) + '<span>Duplicate</span>', onclick: duplicateSelection }),
        el('button', { class: 'btn small icon', title: 'Bring forward (Ctrl+])', 'aria-label': 'Bring forward', html: icon('front', 14), onclick: () => reorder('forward') }),
        el('button', { class: 'btn small icon', title: 'Send backward (Ctrl+[)', 'aria-label': 'Send backward', html: icon('back', 14), onclick: () => reorder('backward') }),
        c.groupId ? el('button', { class: 'btn small', title: 'Ungroup (Ctrl+Shift+G)', html: icon('ungroup', 14) + '<span>Ungroup</span>', onclick: ungroupSelection }) : null,
        el('button', { class: 'btn small danger', title: 'Delete (Del)', html: icon('trash', 14) + '<span>Delete</span>', onclick: deleteSelection })));
    return [head, ident, geo, optSec, notes, styleSec, actions].filter(Boolean);
  }

  function optionFields(c, setOpt, live, done) {
    const o = c.options;
    const f = [];
    switch (c.type) {
      case 'card':
        f.push(el('div', { class: 'field-inline' }, checkCtl('Show title', o.showTitle !== false, (v) => setOpt('showTitle', v))));
        break;
      case 'button':
        f.push(field('Variant', selectCtl(o.variant, [['primary', 'Primary'], ['secondary', 'Secondary'], ['ghost', 'Ghost']], (v) => setOpt('variant', v, true))));
        break;
      case 'chip':
        f.push(el('div', { class: 'field-inline' }, checkCtl('Selected', o.selected, (v) => setOpt('selected', v))));
        break;
      case 'tabs': {
        const labels = splitList(o.tabs);
        f.push(field('Tab labels', textCtl(o.tabs, (v) => { o.tabs = v; live(); }, () => done(true), { placeholder: 'Comma separated' }), { hint: 'Separate tabs with commas.' }));
        f.push(field('Active tab', selectCtl(o.active, labels.map((l, i) => [String(i), l]), (v) => setOpt('active', Number(v)))));
        break;
      }
      case 'toggle':
        f.push(el('div', { class: 'field-inline' }, checkCtl('On', o.on, (v) => setOpt('on', v))));
        break;
      case 'input':
        f.push(field('Kind', selectCtl(o.kind || 'text', [['text', 'Text field'], ['search', 'Search field'], ['dropdown', 'Dropdown']], (v) => setOpt('kind', v))));
        break;
      case 'text':
        f.push(field('Text style', selectCtl(o.variant, [['heading', 'Heading'], ['body', 'Body'], ['caption', 'Caption']], (v) => setOpt('variant', v, true))));
        f.push(field('Alignment', selectCtl(o.align || 'left', [['left', 'Left'], ['center', 'Centre'], ['right', 'Right']], (v) => setOpt('align', v))));
        break;
      case 'media':
        f.push(field('Aspect ratio', selectCtl(o.ratio, [['16:9', '16:9 landscape'], ['9:16', '9:16 vertical']], (v) => {
          if (v !== o.ratio) { const w = c.w; c.w = c.h; c.h = w; }
          o.ratio = v;
          live();
          done(true);
        }), { hint: 'Resizing a media panel keeps its ratio.' }));
        break;
      case 'listrow':
        f.push(field('Subtitle', textCtl(o.subtitle, (v) => { o.subtitle = v; live(); }, () => done())));
        f.push(field('Right-hand text', textCtl(o.meta, (v) => { o.meta = v; live(); }, () => done(), { placeholder: 'e.g. 2 min ago' })));
        f.push(field('Leading element', selectCtl(o.leading || 'none', [['none', 'None'], ['icon', 'Icon'], ['avatar', 'Avatar'], ['checkbox', 'Checkbox']], (v) => setOpt('leading', v, true))));
        if (o.leading === 'checkbox') f.push(el('div', { class: 'field-inline' }, checkCtl('Checked', o.checked, (v) => setOpt('checked', v))));
        break;
      case 'navrail': {
        const items = splitList(o.items);
        f.push(field(`Items (${items.length})`, textCtl(o.items, (v) => { o.items = v; live(); }, () => done(true), { multiline: true, rows: Math.min(8, Math.max(3, items.length)) }), { hint: 'One item per line. Widen the rail past 150 px for a labelled sidebar.' }));
        f.push(field('Active item', selectCtl(o.active, items.map((l, i) => [String(i), l]), (v) => setOpt('active', Number(v)))));
        f.push(el('div', { class: 'field-inline' }, checkCtl('Show labels', o.showLabels !== false, (v) => setOpt('showLabels', v))));
        break;
      }
      case 'pill':
        f.push(field('State', selectCtl(o.state, PILL_STATES, (v) => setOpt('state', v))));
        break;
      case 'progress':
        f.push(field('Value (%)', textCtl(o.value, (v) => { o.value = clamp(num(v), 0, 100); live(); }, () => done(), { type: 'number', min: 0, max: 100 })));
        break;
      case 'chart':
        f.push(field('Chart type', selectCtl(o.chartType, [['line', 'Line'], ['bars', 'Bars']], (v) => setOpt('chartType', v, true))));
        break;
      case 'divider':
        f.push(field('Orientation', selectCtl(o.orientation, [['horizontal', 'Horizontal'], ['vertical', 'Vertical']], (v) => {
          if (v !== o.orientation) { const w = c.w; c.w = c.h; c.h = w; }
          setOpt('orientation', v);
        })));
        break;
      case 'icon':
        f.push(field('Icon', selectCtl(o.glyph, Object.keys(GLYPHS).map((g) => [g, g[0].toUpperCase() + g.slice(1)]), (v) => setOpt('glyph', v))));
        break;
      default:
        break;
    }
    return f;
  }

  function multiPanel(sel) {
    const isGroup = sel.every((c) => c.groupId && c.groupId === sel[0].groupId);
    const head = el('div', { class: 'insp-head' }, el('div', { class: 'type-icon', html: icon('group') }), el('div', {}, el('div', { class: 'ttl' }, isGroup ? 'Group' : `${sel.length} items selected`), el('div', { class: 'sub' }, isGroup ? `${sel.length} items` : 'Shift-click to add or remove')));
    const ab = (k, ic, t) => el('button', { class: 'btn small icon', title: t, 'aria-label': t, html: icon(ic, 14), onclick: () => align(k) });
    const alignSec = section('Align',
      el('div', { class: 'align-grid' }, ab('l', 'alignL', 'Align left'), ab('ch', 'alignCH', 'Align centres horizontally'), ab('r', 'alignR', 'Align right'), ab('t', 'alignT', 'Align top'), ab('cv', 'alignCV', 'Align middles'), ab('b', 'alignB', 'Align bottom')));
    const styleSec = section('Style',
      field('Style set for these items', selectCtl('', [['', 'Choose…'], ['__inherit', 'Page style'], ...allSets().map((s) => [s.id, s.name])], (v) => {
        if (!v) return;
        sel.forEach((c) => { c.styleSetId = v === '__inherit' ? null : v; });
        commit();
        refresh();
        renderInspector();
      })));
    const actions = section('Arrange',
      el('div', { class: 'row-actions' },
        isGroup ? el('button', { class: 'btn small', html: icon('ungroup', 14) + '<span>Ungroup</span>', onclick: ungroupSelection }) : el('button', { class: 'btn small', html: icon('group', 14) + '<span>Group</span>', onclick: groupSelection }),
        el('button', { class: 'btn small', html: icon('copy', 14) + '<span>Duplicate</span>', onclick: duplicateSelection }),
        el('button', { class: 'btn small icon', title: 'Bring forward', 'aria-label': 'Bring forward', html: icon('front', 14), onclick: () => reorder('forward') }),
        el('button', { class: 'btn small icon', title: 'Send backward', 'aria-label': 'Send backward', html: icon('back', 14), onclick: () => reorder('backward') }),
        el('button', { class: 'btn small danger', html: icon('trash', 14) + '<span>Delete</span>', onclick: deleteSelection })));
    const list = section('Selected', el('div', { class: 'layers' }, sel.map((c) => el('button', { class: 'layer sel', html: icon(paletteIcon(c), 14) + `<span class="nm">${esc(c.name)}</span><span class="tp">${esc(TYPES[c.type].label)}</span>`, onclick: () => { selection = new Set([c.id]); renderOverlay(); renderInspector(); renderStatus(); } }))));
    return [head, alignSec, styleSec, actions, list];
  }

  /* ================================================================== token editor */
  const TOKEN_GROUPS = [
    ['Colours', ['background', 'pageTint', 'surface', 'surfaceAlt', 'text', 'muted', 'accent', 'accentText', 'accentSoft', 'border', 'track', 'mediaBg']],
    ['Status colours', ['ok', 'warn', 'danger', 'info']],
    ['Shape', ['radius', 'radiusSm', 'radiusXs', 'borderWidth']],
    ['Effects', ['shadow', 'shadowSm', 'backdrop']],
    ['Type & spacing', ['font', 'spacing']]
  ];
  function openTokenEditor() {
    const pg = page();
    const targetId = pageSetId(pg);
    const src = getSet(targetId);
    const draft = clone(src);
    const isCustom = project.customSets.some((s) => s.id === src.id);
    previewSet = { targetId, set: draft };
    const { body, foot } = openModal({ title: `Style tokens · ${src.name}`, side: true, onClose: () => { previewSet = null; refresh(); renderToolbar(); } });
    const nameIn = el('input', { class: 'inp', value: isCustom ? src.name : 'My ' + src.name, spellcheck: 'false' });
    body.append(el('p', { class: 'hint', style: { margin: '0 0 12px' } }, 'Changes preview live on the canvas. Colours accept any CSS value (hex, rgba, gradients for backgrounds). Save as a new set to keep them; the original set is not changed.'));
    body.append(field('Name of the new set', nameIn));
    const grid = el('div', { class: 'tok-grid' });
    const upd = () => refresh();
    TOKEN_GROUPS.forEach(([title, keys]) => {
      grid.append(el('div', { class: 'tok-group' }, title));
      keys.forEach((k) => {
        const v = draft.tokens[k];
        let ctl;
        if (k === 'spacing') {
          ctl = textCtl((Array.isArray(v) ? v : splitList(v)).join(', '), (val) => { draft.tokens.spacing = splitList(val).map((x) => num(x)); upd(); }, () => {}, { placeholder: '4, 8, 12, 16, 24, 32, 48' });
        } else if (['radius', 'radiusSm', 'radiusXs', 'borderWidth'].includes(k)) {
          ctl = textCtl(num(v), (val) => { draft.tokens[k] = num(val) + 'px'; upd(); }, () => {}, { type: 'number', min: 0, step: k === 'borderWidth' ? 0.5 : 1 });
        } else if (['shadow', 'shadowSm', 'backdrop', 'font'].includes(k)) {
          ctl = textCtl(v, (val) => { draft.tokens[k] = val; upd(); }, () => {});
        } else {
          ctl = colorCtl(v, (val) => { draft.tokens[k] = val || src.tokens[k]; upd(); }, () => {});
        }
        grid.append(el('label', {}, k), ctl);
      });
    });
    body.append(grid);
    const saveNew = () => {
      const set = clone(draft);
      set.id = 'custom-' + uid();
      set.name = nameIn.value.trim() || src.name + ' (custom)';
      set.description = `Custom set based on ${src.name}.`;
      project.customSets.push(set);
      if (pg.styleSetId) pg.styleSetId = set.id; else project.styleSetId = set.id;
      previewSet = null;
      closeModal();
      commit();
      renderAll();
      toast(`Saved style set “${set.name}”`);
    };
    const updateExisting = () => {
      const i = project.customSets.findIndex((s) => s.id === src.id);
      const set = clone(draft);
      set.name = nameIn.value.trim() || src.name;
      project.customSets[i] = set;
      previewSet = null;
      closeModal();
      commit();
      renderAll();
      toast('Style set updated');
    };
    foot.append(
      el('button', { class: 'btn', title: 'Download styles-library.json with all sets, including your custom ones', html: icon('save', 14) + '<span>Library</span>', onclick: () => downloadLibrary() }),
      el('span', { class: 'grow' }),
      el('button', { class: 'btn', onclick: () => closeModal() }, 'Cancel'),
      ...(isCustom ? [el('button', { class: 'btn', onclick: updateExisting }, 'Update')] : []),
      el('button', { class: 'btn primary', onclick: saveNew }, 'Save as new set'));
  }
  function downloadLibrary() {
    const lib = Object.assign({}, clone(library), { sets: [...clone(library.sets), ...clone(project.customSets)] });
    download('styles-library.json', JSON.stringify(lib, null, 2) + '\n');
    toast('Downloaded styles-library.json');
  }

  /* ================================================================== export / import */
  function readingOrder(list) {
    const sorted = [...list].sort((a, b) => a.y - b.y || a.x - b.x);
    const rows = [];
    for (const c of sorted) {
      const r = rows[rows.length - 1];
      if (r && c.y < r.top + 16) r.items.push(c); else rows.push({ top: c.y, items: [c] });
    }
    return rows.flatMap((r) => r.items.sort((a, b) => a.x - b.x));
  }
  function orderTree(pg) {
    const list = pg.components;
    const z = new Map(list.map((c, i) => [c.id, i]));
    const parent = new Map();
    for (const c of list) {
      let best = null;
      for (const p of list) {
        if (p === c || p.type !== 'card') continue;
        if (z.get(p.id) > z.get(c.id)) continue;
        const inside = c.x >= p.x - 1 && c.y >= p.y - 1 && c.x + c.w <= p.x + p.w + 1 && c.y + c.h <= p.y + p.h + 1;
        if (inside && (!best || p.w * p.h < best.w * best.h)) best = p;
      }
      if (best) parent.set(c.id, best);
    }
    const kids = new Map();
    const top = [];
    for (const c of list) {
      const p = parent.get(c.id);
      if (p) { if (!kids.has(p.id)) kids.set(p.id, []); kids.get(p.id).push(c); } else top.push(c);
    }
    const out = [];
    const walk = (nodes, depth) => readingOrder(nodes).forEach((c) => { out.push({ c, depth, parent: parent.get(c.id) || null, z: z.get(c.id) }); if (kids.has(c.id)) walk(kids.get(c.id), depth + 1); });
    walk(top, 0);
    return out;
  }
  function buildLayout() {
    const used = new Set([getSet(project.styleSetId).id]);
    project.pages.forEach((pg) => { if (pg.styleSetId) used.add(pg.styleSetId); pg.components.forEach((c) => { if (c.styleSetId) used.add(c.styleSetId); }); });
    return {
      format: 'nightshift-layout',
      version: 1,
      project: project.name,
      exportedAt: new Date().toISOString(),
      units: 'px',
      grid: GRID,
      defaultStyleSet: getSet(project.styleSetId).id,
      pageTint: !!project.tint,
      styleSets: [...used].map((id) => { const s = getSet(id); return { id: s.id, name: s.name, scheme: s.scheme || 'light', description: s.description || '', tokens: s.tokens, components: s.components }; }),
      pages: project.pages.map((pg) => ({
        id: pg.id,
        name: pg.name,
        width: pg.w,
        height: pg.h,
        styleSet: getSet(pageSetId(pg)).id,
        notes: pg.notes,
        components: orderTree(pg).map(({ c, parent, z }) => ({
          id: c.id, type: c.type, name: c.name, label: c.label,
          x: c.x, y: c.y, width: c.w, height: c.h, z,
          options: c.options, notes: c.notes,
          styleSet: c.styleSetId || null, overrides: c.overrides,
          group: c.groupId || null, parent: parent ? parent.id : null
        }))
      }))
    };
  }
  function describe(c) {
    const o = c.options;
    const q = (s) => `“${String(s).replace(/\s+/g, ' ').trim()}”`;
    switch (c.type) {
      case 'card': return 'Card' + (o.showTitle !== false && c.label ? ` titled ${q(c.label)}` : ' (no title)');
      case 'button': return `${o.variant[0].toUpperCase() + o.variant.slice(1)} button ${q(c.label)}`;
      case 'chip': return `Chip/badge ${q(c.label)}${o.selected ? ', selected' : ''}`;
      case 'tabs': { const l = splitList(o.tabs); return `Tabs: ${l.map((t, i) => (i === Number(o.active) ? `${t} (active)` : t)).join(', ')}`; }
      case 'toggle': return `Toggle ${q(c.label)}, ${o.on ? 'on' : 'off'} by default`;
      case 'input': return `${{ text: 'Text field', search: 'Search field', dropdown: 'Dropdown' }[o.kind] || 'Text field'} with placeholder ${q(c.label)}`;
      case 'text': return `${{ heading: 'Heading', body: 'Body text', caption: 'Caption' }[o.variant] || 'Text'} ${q(c.label)}${o.align && o.align !== 'left' ? `, ${o.align}-aligned` : ''}`;
      case 'media': return `${o.ratio === '9:16' ? 'Vertical 9:16' : 'Landscape 16:9'} media panel ${q(c.label)} with a play button`;
      case 'listrow': {
        const bits = [`List row ${q(c.label)}`];
        if (o.subtitle) bits.push(`subtitle ${q(o.subtitle)}`);
        if (o.meta) bits.push(`right-hand text ${q(o.meta)}`);
        if (o.leading && o.leading !== 'none') bits.push(o.leading === 'checkbox' ? `checkbox (${o.checked ? 'ticked' : 'empty'})` : `leading ${o.leading}`);
        return bits.join(', ');
      }
      case 'navrail': { const l = splitList(o.items); return `Vertical nav rail with ${l.length} items: ${l.map((t, i) => (i === Number(o.active) ? `${t} (active)` : t)).join(', ')}${o.showLabels === false ? ' (icons only)' : ''}`; }
      case 'pill': return `${(PILL_STATES.find((s) => s[0] === o.state) || ['', 'Neutral'])[1]} status pill ${q(c.label)}`;
      case 'progress': return `Progress bar at ${clamp(num(o.value), 0, 100)} %`;
      case 'chart': return `${o.chartType === 'bars' ? 'Bar' : 'Line'} chart${c.label ? ` titled ${q(c.label)}` : ''}`;
      case 'image': return `Image placeholder${c.label ? ` (${q(c.label)})` : ''}`;
      case 'divider': return `${o.orientation === 'vertical' ? 'Vertical' : 'Horizontal'} divider`;
      case 'icon': return `Icon (${o.glyph})`;
      default: return TYPES[c.type].label;
    }
  }
  function buildSpec() {
    const lines = [];
    const d0 = new Date();
    const date = `${d0.getFullYear()}-${String(d0.getMonth() + 1).padStart(2, '0')}-${String(d0.getDate()).padStart(2, '0')}`;
    const dflt = getSet(project.styleSetId);
    lines.push(`# ${project.name} · UI spec`, '');
    lines.push(`Exported from the NightShift UI sketchpad on ${date}. Default style set: **${dflt.name}**${project.tint ? ' with page tint' : ''}.`);
    lines.push('Positions are pixels from the top-left corner of the page (x, y); sizes are width × height. Components are listed in reading order: top to bottom, left to right. Indented items sit inside the card above them.', '');
    project.pages.forEach((pg, pi) => {
      const set = getSet(pageSetId(pg));
      lines.push(`## ${pi + 1}. ${pg.name} (${pg.w} × ${pg.h})`, '');
      if (pg.notes) lines.push(`> ${pg.notes.replace(/\n+/g, ' ')}`, '');
      if (pg.styleSetId) lines.push(`Style set for this page: **${set.name}**.`, '');
      const tree = orderTree(pg);
      if (!tree.length) { lines.push('_This page is empty._', ''); return; }
      const groups = {};
      let gi = 0;
      let n = 0;
      tree.forEach(({ c, depth }) => {
        const pad = '   '.repeat(depth);
        const bullet = depth === 0 ? `${++n}.` : '-';
        let line = `${pad}${bullet} **${c.name}**: ${describe(c)}. At x ${c.x}, y ${c.y}; ${c.w} × ${c.h}.`;
        if (c.groupId) { if (!groups[c.groupId]) groups[c.groupId] = String.fromCharCode(65 + (gi++ % 26)); line += ` Group ${groups[c.groupId]}.`; }
        if (c.styleSetId) line += ` Style set: ${getSet(c.styleSetId).name}.`;
        const ov = Object.entries(c.overrides || {});
        if (ov.length) line += ` Overrides: ${ov.map(([k, v]) => `${k} ${v}${k === 'radius' || k === 'fontSize' ? ' px' : ''}`).join(', ')}.`;
        lines.push(line);
        if (c.notes) lines.push(`${' '.repeat(pad.length + (depth === 0 ? 3 : 2))}What it does: ${c.notes.replace(/\n+/g, ' ')}`);
      });
      lines.push('');
    });
    return lines.join('\n');
  }
  function openExport() {
    const layout = JSON.stringify(buildLayout(), null, 2) + '\n';
    const spec = buildSpec() + '\n';
    const base = slug(project.name);
    download('layout.json', layout);
    setTimeout(() => download('spec.md', spec, 'text/markdown'), 350);
    const { body, foot } = openModal({ title: 'Export' });
    let cur = 'layout';
    const ta = el('textarea', { class: 'code', readonly: 'readonly', spellcheck: 'false', 'aria-label': 'Export preview' });
    const seg = el('div', { class: 'seg', role: 'tablist' });
    const tabs = { layout: 'layout.json', spec: 'spec.md' };
    const show = (k) => {
      cur = k;
      ta.value = k === 'layout' ? layout : spec;
      seg.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.k === k));
    };
    Object.entries(tabs).forEach(([k, label]) => seg.append(el('button', { 'data-k': k, role: 'tab', onclick: () => show(k) }, label)));
    body.append(el('div', { style: { display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' } }, seg, el('span', { class: 'hint' }, 'Both files were downloaded. Hand them to the team, or copy from here.')), ta);
    show('layout');
    foot.append(
      el('span', { class: 'hint' }, `${project.pages.length} pages · ${project.pages.reduce((a, p) => a + p.components.length, 0)} components`),
      el('span', { class: 'grow' }),
      el('button', { class: 'btn', html: icon('save', 14) + '<span>Download again</span>', onclick: () => (cur === 'layout' ? download('layout.json', layout) : download('spec.md', spec, 'text/markdown')) }),
      el('button', { class: 'btn primary', html: icon('copy', 14) + '<span>Copy</span>', onclick: async () => { toast((await copyText(ta.value)) ? `Copied ${tabs[cur]}` : 'Copy failed; select the text and press Ctrl+C'); } }));
    void base;
  }
  function saveProjectFile() {
    const data = Object.assign({ format: 'nightshift-design-project', version: 1, savedAt: new Date().toISOString() }, project);
    download(slug(project.name) + '.project.json', JSON.stringify(data, null, 2) + '\n');
    toast('Project saved as a file');
  }
  function layoutToProject(L) {
    const libIds = new Set(library.sets.map((s) => s.id));
    const customSets = (L.styleSets || []).filter((s) => s && s.id && !libIds.has(s.id) && s.tokens).map((s) => ({ id: s.id, name: s.name || s.id, scheme: s.scheme || 'light', description: s.description || '', tokens: s.tokens, components: s.components || {} }));
    const dflt = L.defaultStyleSet || 'bento-light';
    return {
      name: L.project || 'Imported layout',
      styleSetId: dflt,
      tint: !!L.pageTint,
      customSets,
      pages: (L.pages || []).map((p) => ({
        id: p.id, name: p.name, w: p.width, h: p.height,
        styleSetId: p.styleSet && p.styleSet !== dflt ? p.styleSet : null,
        notes: p.notes || '',
        components: [...(p.components || [])].sort((a, b) => num(a.z) - num(b.z)).map((c) => ({
          id: c.id, type: c.type, name: c.name, label: c.label, x: c.x, y: c.y, w: c.width, h: c.height,
          options: c.options, notes: c.notes, styleSetId: c.styleSet || null, overrides: c.overrides || {}, groupId: c.group || null
        }))
      }))
    };
  }
  function openFile(mode) {
    const fi = $('#fileInput');
    fi.value = '';
    fi.dataset.mode = mode;
    fi.click();
  }
  $('#fileInput').addEventListener('change', async (ev) => {
    const file = ev.target.files && ev.target.files[0];
    if (!file) return;
    let data;
    try { data = JSON.parse(await file.text()); } catch (err) { toast('That file is not valid JSON'); return; }
    if (data && data.format === 'nightshift-layout') {
      replaceProject(layoutToProject(data), `Imported ${file.name}`);
    } else if (data && Array.isArray(data.pages)) {
      if (data.pages[0] && data.pages[0].components && data.pages[0].components[0] && data.pages[0].components[0].width != null) replaceProject(layoutToProject(data), `Imported ${file.name}`);
      else replaceProject(data, `Opened ${file.name}`);
    } else if (data && data.format === 'nightshift-style-library' && Array.isArray(data.sets)) {
      const ids = new Set(allSets().map((s) => s.id));
      const extra = data.sets.filter((s) => s.id && s.tokens && !ids.has(s.id));
      project.customSets.push(...extra);
      commit();
      renderAll();
      toast(extra.length ? `Added ${extra.length} style set(s)` : 'No new style sets in that file');
    } else {
      toast('Not a project, layout.json or style library file');
    }
  });

  /* ================================================================== shortcuts panel */
  function openShortcuts() {
    const { body, foot } = openModal({ title: 'Keyboard shortcuts' });
    const K = (...keys) => el('span', { class: 'keys' }, keys.map((k) => el('kbd', {}, k)));
    const row = (label, ...keys) => el('div', { class: 'sc' }, el('span', {}, label), K(...keys));
    body.append(el('div', { class: 'shortcuts' },
      el('h3', {}, 'Edit'),
      row('Undo', 'Ctrl', 'Z'), row('Redo', 'Ctrl', 'Y'),
      row('Duplicate', 'Ctrl', 'D'), row('Delete', 'Del'),
      row('Copy', 'Ctrl', 'C'), row('Paste (also on another page)', 'Ctrl', 'V'),
      row('Cut', 'Ctrl', 'X'), row('Select all', 'Ctrl', 'A'),
      row('Edit label of selection', 'Enter'), row('Deselect', 'Esc'),
      el('h3', {}, 'Arrange'),
      row('Nudge 1 px', '←', '→', '↑', '↓'), row('Nudge 8 px', 'Shift', 'Arrow'),
      row('Group', 'Ctrl', 'G'), row('Ungroup', 'Ctrl', 'Shift', 'G'),
      row('Bring forward', 'Ctrl', ']'), row('Send backward', 'Ctrl', '['),
      row('Bring to front', 'Ctrl', 'Shift', ']'), row('Send to back', 'Ctrl', 'Shift', '['),
      el('h3', {}, 'Mouse'),
      row('Keep ratio while resizing', 'Shift', 'drag'), row('Move on one axis', 'Shift', 'drag'),
      row('Ignore snapping', 'Alt', 'drag'), row('Add to selection', 'Shift', 'click'),
      row('Marquee select', 'drag', 'empty area'), row('Rename page', 'double-click', 'tab'),
      el('h3', {}, 'View & file'),
      row('Pan', 'Space', 'drag'), row('Zoom', 'Ctrl', 'scroll'),
      row('Zoom in / out', 'Ctrl', '+', '−'), row('Zoom 100 %', 'Ctrl', '0'),
      row('Fit page', 'Shift', '1'), row('This panel', '?'),
      row('Save project file', 'Ctrl', 'S'), row('Export', 'Ctrl', 'E')));
    foot.append(el('span', { class: 'hint' }, 'On a Mac, use Cmd instead of Ctrl.'), el('span', { class: 'grow' }), el('button', { class: 'btn primary', onclick: () => closeModal() }, 'Done'));
  }

  /* ================================================================== toolbar wiring */
  async function newProject() {
    if (!(await confirmDialog('Start a new project?', 'This clears the canvas and starts with seven empty pages. Save the current project first if you want to keep it. You can also undo this.', 'New project', true))) return;
    const p = newProjectData();
    p.styleSetId = project.styleSetId;
    p.customSets = clone(project.customSets);
    replaceProject(p, 'New project');
  }
  const ACTIONS = {
    new: newProject,
    open: () => openFile('open'),
    save: saveProjectFile,
    import: () => openFile('import'),
    export: openExport,
    undo,
    redo,
    tokens: openTokenEditor,
    zoomIn: () => zoomStep(1),
    zoomOut: () => zoomStep(-1),
    zoom100: () => setZoom(1),
    zoomFit,
    help: openShortcuts
  };
  function wireStatic() {
    document.querySelectorAll('[data-icon]').forEach((b) => b.insertAdjacentHTML('afterbegin', icon(b.dataset.icon, b.classList.contains('brand-mark') ? 16 : 16)));
    document.querySelectorAll('[data-action]').forEach((b) => b.addEventListener('click', () => { closeMenu(); ACTIONS[b.dataset.action](); }));
    document.querySelectorAll('[data-toggle]').forEach((b) => b.addEventListener('click', () => {
      const k = b.dataset.toggle;
      if (k === 'tint') { project.tint = !project.tint; commit(); }
      else { ui[k] = !ui[k]; saveUi(); }
      renderToolbar();
      refresh();
      if (!selected().length) renderInspector();
      toast(`${{ tint: 'Page tint', grid: 'Grid', snap: 'Snapping', rulers: 'Rulers' }[k]} ${(k === 'tint' ? project.tint : ui[k]) ? 'on' : 'off'}`);
    }));
    $('#styleSelect').addEventListener('change', (ev) => {
      project.styleSetId = ev.target.value;
      commit();
      refresh();
      renderInspector();
      toast(`Style: ${getSet(project.styleSetId).name}`);
    });
    const nameInput = $('#projectName');
    nameInput.addEventListener('input', () => { project.name = nameInput.value; });
    nameInput.addEventListener('change', () => { if (!project.name.trim()) project.name = 'Untitled app'; commit(); renderToolbar(); });
    nameInput.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') nameInput.blur(); });
    viewport.tabIndex = -1;
  }

  /* ================================================================== boot */
  async function fetchJson(path) {
    if (location.protocol === 'file:') return null;
    try {
      const r = await fetch(path, { cache: 'no-store' });
      return r.ok ? await r.json() : null;
    } catch (err) {
      return null;
    }
  }
  async function boot() {
    loadUi();
    wireStatic();
    const lib = await fetchJson('styles-library.json');
    library = lib && Array.isArray(lib.sets) && lib.sets.length ? lib : clone(EMBEDDED_LIBRARY || { sets: [] });
    if (!library.sets.length) library.sets = [{ id: 'plain', name: 'Plain', tokens: { background: '#fff', surface: '#fff', text: '#111', muted: '#777', accent: '#2563eb', accentText: '#fff', border: '#ddd', radius: '8px', radiusSm: '6px', font: 'system-ui, sans-serif', spacing: [4, 8, 12, 16, 24, 32, 48] }, components: {} }];
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch (err) { stored = null; }
    let data = stored;
    if (!data) data = (await fetchJson('sample-project.json')) || (EMBEDDED_SAMPLE ? clone(EMBEDDED_SAMPLE) : null);
    project = normalizeProject(data || newProjectData());
    lastSnap = snapshot();
    if (!stored) persist();
    renderPalette();
    renderAll();
    requestAnimationFrame(zoomFit);
  }
  boot();
})();
