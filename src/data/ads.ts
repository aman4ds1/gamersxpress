// Controls whether ad placeholders render on the site.
//
// To enable ads later:
//   1. Set ADS_ENABLED to true.
//   2. Replace the placeholder div inside src/components/AdSlot.astro with the
//      ad network's tag for each variant (article-top, article-inline,
//      sidebar). Keep the fixed-width, fixed-height wrapper so the reserved
//      space does not shift the layout while the ad loads.
//
// While ADS_ENABLED is false, AdSlot renders no markup at all, so the site
// loads no ad scripts and shows nothing to visitors.
export const ADS_ENABLED = false;