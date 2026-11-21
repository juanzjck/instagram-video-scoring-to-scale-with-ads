# Case study: AI creative scoring in marketing (Kellanova and Vidmob)

## Introduction

Machine learning and modern generative AI are breakthroughs in many industries and are part of our daily lives. Companies are leveraging AI in different ways, such as software development, customer service, or content generation; furthermore, companies are extending the use of this technology to create more efficient content that catches the attention of prospective customers. This information will focus on Kellanova and Vidmob cases. For one side, Kellanova, an American food company, and Vidmob, a tech company, focus on data analysis and AI and together work on content analysis and the use of AI to take decisions regarding the content created for marketing campaigns.

## The problem

Content creation for marketing purposes tends to be treated as an art and is challenging to measure and ensure results. This approach makes sense because there are many variables to consider before creating content and designing a campaign, especially in large companies like Kellanova that run hundreds of campaigns and must evaluate them manually. The marketing teams are overloaded with work and still can't ensure results, leading to significant losses.

## AI Solution Implemented

Kallanova implements a scoring system that measures the features that make content catch the attention of possible customers; the sources describe the thesis steps:

- **Collect data:** Kallanova ran 443+ campaigns within a year of duration, then linked the results with the ads themselves.
- **Tag each ad with creative features:** Vidmod analyzed 20,000 content decisions, for instance, copies, colors, brand positions, and overlay text on the video or shots of the person eating the product.
- **Determine what drives results:** the models predict that some features are more effective than others depending on the category of the product; for instance, salty snacks are more likely to perform well with videos where the person is eating, and a difference in a sweet product performs better on indulgent content that shows the product in a vivid way. The models identify and narrow 20000 decisions into 19 key decision criteria. To get these results, they use a clear, unique KPI, the 3-second view rate.
- **Score:** score each ad based on predicted 3-second view rates, enabling real-time identification of strong and weak ads for campaign decisions.
- **Integrate it into the workflow:** A cross-functional team worked on the scoring, prioritizing the criteria from must-have to nice-to-have, and consulted a human for final approval. Before launching any campaign, agencies utilize the score to ensure that ads' scores fall within a specific threshold. The framework is being rerun for other platforms, such as TikTok.

## Challenges and Obstacles

There were some challenges, principally the resistance from some teams that distrusted the use of AI and preferred gut instincts; this resistance came along with the fear of trying to measure creativity, leading to a lack of creativity because of the constraints imposed by a machine.

Another challenge was that this approach and this system were quite new; executives mentioned that they were "scratching the surface." The change also faced challenges with the team alignment; they had to agree on the 19 key criteria.

Finally, they had to rerun the framework because they stated with Meta ads, and the model doesn't count with data from other social media; the next step was to rerun it with TikTok.

## Results and Impact

Kellanova uses the model to forecast the 3-second retention rate with 83% accuracy, the creative performance increase 2.16x, and a 11% increase in ROI.

| Result | Reported figure |
|---|---|
| Creative performance | 2.16x (Marketing Dive says "more than 2x") |
| Analysis speed | About 20,000 creative decisions analyzed in hours |
| ROI | +11% for scored assets |
| Prediction accuracy | 83% on 3-second view-through rate |
| Conversion | 4x conversion for creative that followed the recommendations |

The implementation of this system had a positive impact; the capacity to forecast the attention of the people within the first 3 seconds helps marketing and creative teams to take better decisions regarding the marketing assets.

## Lessons for PartGram

PartGram's value proposition is to reduce the CAC, and to reach this goal, PartGram needs to ensure that the system provides the information to make the best decision regarding the content that the company will scale.

This case study addresses this issue, but PartGram does not need to replicate it; instead, it has its own accuracy threshold that companies can use for validation. The following are some insights that might be useful to the program:

| Lesson | Evidence |
|---|---|
| Keep the feature list short | The case mentions that from 20000 decisions they narrowed it down to 19. |
| Validate against real outcomes, not proxies. Attach the measure to real and wanted results. In Pargram, the CAC (Cost Acquisition Costumer) could be the cost per conversion. | Kroger moved from view rate to actual purchases and still reached 81% |
| Use the score as a gate, but keep human override | Kellanova agencies must clear a threshold, yet executives say it does not dictate creative |
| Different platforms require reruns. | Kellanova is required to rerun on different platform, such as TikTok, because of its differences. |
| Keep clear what the score rewards, avoiding narrowing and constraining the diversity and creativity. | Kellanova mentioned that this is a start. Agencies were worried be constrain creatively. |

## Different approaches in the current market

| Approach | Score logic | Reported evidence | Caveat |
|---|---|---|---|
| Vidmob (learned criteria) | Models learn which creative features drive a chosen KPI, then become pass/fail criteria. | 83% accuracy on 3-second view rate (Kellanova); 81% on conversion (Kroger) | Accuracy method, baselines and holdout not disclosed; vendor and partner ran the study. |
| Pencil ([source](https://trypencil.co/blog/articles/pencil-media-performance-score)) (similarity) | 0–100 score from the real results of visually similar ads in its database; predicts click-through rate only | 61% of 7,000+ ads landed in the right decile band or one band away. | Company-run validation; no holdout or time split described; correlation printed as both 70.5 and 0.705. |
| CreativeX ([source](https://www.mediapost.com/publications/article/366907/building-a-creative-quality-score-into-campaign-re.html)) (checklist) | Share of validated best practices an asset follows | Per 10% higher score: CPM down about 2%, cost per completed view down 4.8–5.7% (about 1M ads, $1B spend, 2021 report) | Correlation across ads, not a per-ad forecast; summarized by a trade article, not the full report. |

## Conclusion and comments

As a conclusion, this case study shows the implementation of AI in a creative process had an important impact on how companies make marketing and content creation decisions. The main idea was to improve the results by data-driven decisions; this means that the implementation doesn't replace humans, but it can be used as a tool that improves the performance and reacts to wanted outcomes.

The capstone project is closely related to this case study, and the insights in this report will guide the third iteration.

## Sources

- Business Wire: Vidmob, Kellanova and MMA Global predictive impact scoring study
- Marketing Dive: How Kellanova uses AI to predict creative performance (Sept 24, 2025)
- Grocery Dive: Kroger, AI and predictive scoring for e-commerce (Oct 1, 2026)
- Pencil: Media performance score
- MediaPost: Building a creative quality score into campaign review (Sept 14, 2021)
- Potdar (2024): Integration of AI in content creation for digital marketing startups
- Imran et al.: Generative AI in visual content marketing
- Kim et al. (2026): Content creation with generative AI, ACM CSCW, metadata via Crossref
- Bathe et al. (2026): Dynamic content creation with AI and APIs
- PartGram capstone proposal and Section 5 plan (project files)
